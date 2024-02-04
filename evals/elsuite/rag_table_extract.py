import os
import traceback
from io import StringIO
import json
import re
from pathlib import Path

from typing import List, Optional, Tuple, Union

import pandas as pd
import numpy as np
from pydantic import BaseModel
import uuid

import evals
import evals.metrics
from evals.api import CompletionFn
from evals.elsuite.rag_match import get_rag_dataset
from evals.record import RecorderBase, record_match


code_pattern = r"```[\s\S]*?\n([\s\S]+?)\n```"
json_pattern = r"```json[\s\S]*?\n([\s\S]+?)\n```"
csv_pattern = r"```csv[\s\S]*?\n([\s\S]+?)\n```"
outlink_pattern = r"\[Download[a-zA-Z0-9 ]+?\]\((https://[a-zA-Z0-9_. /]+?)\)"


def parse_csv_text(csvtext: str) -> str:
    lines = csvtext.strip().split("\n")
    tuple_pattern = r"\((\"[\s\S]*?\"),(\"[\s\S]*?\")\)"
    if re.search(tuple_pattern, lines[0]) is not None:
        lines[0] = re.sub(tuple_pattern, r"(\1|\2)", lines[0])
    lines_clr = [re.sub(r"\"[\s\S]*?\"", "", line) for line in lines]
    max_commas = max([line_clr.count(",") for line_clr in lines_clr])
    unified_lines = [line + ("," * (max_commas - line_clr.count(","))) for line, line_clr in zip(lines, lines_clr)]
    return "\n".join(unified_lines)


def parse_table_multiindex(table: pd.DataFrame, compare_fields: list = []) -> pd.DataFrame:
    """
    Parse a table with multiindex columns.
    """

    df = table.copy()
    if df.columns.nlevels == 1 and tuple in [type(f) for f in compare_fields]:
        coltypes = {col: type(df[col].iloc[0]) for col in df.columns}
        for col, ctype in coltypes.items():
            if ctype == str:
                if ":" in df[col].iloc[0] and "," in df[col].iloc[0]:
                    df[col] = [{key: value for key, value in [pair.split(": ") for pair in data.split(", ")]} for data
                               in df[col]]
                    coltypes[col] = dict
        dfs = []

        for col, ctype in coltypes.items():
            if ctype == dict:
                d = pd.DataFrame(df.pop(col).tolist())
                d.columns = pd.MultiIndex.from_tuples([(col, fuzzy_normalize(key)) for key in d.columns])
                dfs.append(d)
        df.columns = pd.MultiIndex.from_tuples([eval(col.replace("|", ",")) if (col[0] == "(" and col[-1] == ")") else
                                                (col, "") for col in df.columns])
        df = pd.concat([df] + dfs, axis=1)
    if df.columns.nlevels > 1:
        df.columns = pd.MultiIndex.from_tuples([(col, fuzzy_normalize(subcol)) for col, subcol in df.columns])

    return df


class FileSample(BaseModel):
    file_name: Optional[str]
    file_link: Optional[str]
    answerfile_name: Optional[str]
    answerfile_link: Optional[str]
    compare_fields: List[Union[str, Tuple]]
    index: Union[str, Tuple] = ("Compound", "")


def fuzzy_compare(a: str, b: str) -> Union[bool, float]:
    """
    Compare two strings with fuzzy matching.
    """

    def standardize_unit(s: str) -> str:
        """
        Standardize a (affinity) string to common units.
        """
        mark = "" if re.search(r"[><=]", s) is None else re.search(r"[><=]", s).group()
        unit = s.rstrip()[-2:]
        number = float(re.search(r"[\+\-]*[0-9.]+", s).group())

        if unit in ["µM", "uM"]:
            unit = "nM"
            number *= 1000
        elif unit in ["mM", "mm"]:
            unit = "nM"
            number *= 1000000

        if mark == "=":
            mark = ""
        return f"{mark}{number:.1f} {unit}"

    def is_float(str):
        try:
            float(str)
            return True
        except ValueError:
            return False

    unit_str = ["nM", "uM", "µM", "mM", "%", " %"]
    nan_str = ["n/a", "nan", "na", "n.a.", "nd", "not determined", "not tested", "inactive"]
    a = a.strip()
    b = b.strip()
    if is_float(a) and is_float(b):
        return np.allclose(float(a), float(b), equal_nan=True, atol=1e-2, rtol=1e-2)
    elif (a[-2:] in unit_str or a[-1] in unit_str) and (b[-2:] in unit_str or b[-1] in unit_str):
        a = standardize_unit(a)
        b = standardize_unit(b)
        return a == b
    elif a.lower() in nan_str and b.lower() in nan_str:
        return True
    elif (a.lower() in b.lower()) or (b.lower() in a.lower()):
        return True
    else:
        import Levenshtein
        return Levenshtein.distance(a.lower(), b.lower()) / (len(a) + len(b)) < 0.1


def fuzzy_normalize(s):
    if s.startswith("Unnamed"):
        return ""
    else:
        """ 标准化字符串 """
        # 定义需要移除的单位和符号
        units = ["µM", "µg/mL", "nM"]
        for unit in units:
            s = s.replace(unit, "")

        # 定义特定关键字
        keywords = ["pIC50", "IC50", "EC50", "TC50", "GI50", "Ki", "Kd", "Kb", "pKb"]

        # 移除非字母数字的字符，除了空格
        # s = re.sub(r'[^\w\s]', '', s)

        # 分割字符串为单词列表
        words = s.split()

        # 将关键字移到末尾
        reordered_words = [word for word in words if word not in keywords]
        keywords_in_string = [word for word in words if word in keywords]
        reordered_words.extend(keywords_in_string)
        # 重新组合为字符串
        return ' '.join(reordered_words)


class TableExtract(evals.Eval):
    def __init__(
            self,
            completion_fns: list[CompletionFn],
            samples_jsonl: str,
            *args,
            instructions: Optional[str] = "",
            **kwargs,
    ):
        super().__init__(completion_fns, *args, **kwargs)
        assert len(completion_fns) < 3, "TableExtract only supports 3 completion fns"
        self.samples_jsonl = samples_jsonl
        self.instructions = instructions

    def eval_sample(self, sample, rng):
        assert isinstance(sample, FileSample)

        prompt = \
                self.instructions
                # + f"\nThe fields should at least contain {sample.compare_fields}"
        result = self.completion_fn(
            prompt=prompt,
            temperature=0.0,
            max_tokens=5,
            file_name=sample.file_name,
            file_link=sample.file_link
        )
        sampled = result.get_completions()[0]

        compare_fields_types = [type(x) for x in sample.compare_fields]
        header_rows = [0, 1] if tuple in compare_fields_types else [0]

        correct_answer = parse_table_multiindex(pd.read_csv(sample.answerfile_name, header=header_rows).astype(str), compare_fields=sample.compare_fields)
        correct_answer.to_csv("temp.csv", index=False)
        correct_str = open("temp.csv", 'r').read()

        try:
            if re.search(outlink_pattern, sampled) is not None:
                code = re.search(outlink_pattern, sampled).group()
                link = re.sub(outlink_pattern, r"\1", code)

                fname = f"/tmp/LLMEvals_{uuid.uuid4()}.csv"
                os.system(f"wget {link} -O {fname}")
                table = pd.read_csv(fname)
                if pd.isna(table.iloc[0, 0]):
                    table = pd.read_csv(fname, header=header_rows)
            elif "csv" in prompt:
                code = re.search(csv_pattern, sampled).group()
                code_content = re.sub(csv_pattern, r"\1", code)
                code_content_processed = parse_csv_text(code_content)
                # table = pd.read_csv(StringIO(code_content_processed), header=header_rows)
                table = pd.read_csv(StringIO(code_content_processed))
                if pd.isna(table.iloc[0, 0]):
                    table = pd.read_csv(StringIO(code_content_processed), header=header_rows)

            elif "json" in prompt:
                code = re.search(json_pattern, sampled).group()
                code_content = re.sub(json_pattern, r"\1", code).replace("\"", "")
                table = pd.DataFrame(json.loads(code_content))
            else:
                table = pd.DataFrame()
            table = parse_table_multiindex(table, compare_fields=sample.compare_fields)

            if sample.index not in table.columns:
                table.columns = [sample.index] + list(table.columns)[1:]
            answerfile_out = sample.answerfile_name.replace(".csv", "_output.csv")
            table.to_csv(answerfile_out, index=False)
            picked_str = open(answerfile_out, 'r').read()
        except:
            print(Path(sample.file_name).stem)
            traceback.print_exc()
            record_match(
                prompt=prompt,
                correct=False,
                expected=correct_str,
                picked=sampled,
                file_name=sample.file_name,
                jobtype="match_all"
            )
            return

        # TODO: Use similarity and Bipartite matching to match fields
        renames = {}
        for field in sample.compare_fields:
            for i, sample_field in enumerate(table.columns):
                field_query = field if type(field) != tuple else field[0] if field[1] == "" else field[1]
                sample_field_query = sample_field if type(sample_field) != tuple else sample_field[0] if sample_field[1] == "" else sample_field[1]
                if fuzzy_normalize(field_query) == "" or fuzzy_normalize(sample_field_query) == "":
                    continue
                if fuzzy_compare(fuzzy_normalize(field_query), fuzzy_normalize(sample_field_query)) and \
                        fuzzy_normalize(field_query).split()[-1] == fuzzy_normalize(sample_field_query).split()[-1]:
                    if sample_field not in renames.keys() and field_query not in renames.values():
                        renames[sample_field_query] = field_query
                        break
        renames = {key: value for key, value in renames.items() if key not in ["Compound", "Name", "SMILES"]}
        if len(renames) > 0:
            print("Find similar fields between answer and correct:", renames)
            table.rename(columns=renames, inplace=True)
            print(table)

        table[sample.index] = table[sample.index].astype(str)
        correct_answer[sample.index] = correct_answer[sample.index].astype(str)
        comparison_df = pd.merge(table.set_index(sample.index, drop=False),
                                 correct_answer.set_index(sample.index, drop=False),
                                 how="right", left_index=True, right_index=True)

        match_all = True
        for field in sample.compare_fields:
            if type(field) == tuple and len(field) > 1:
                field_sample, field_correct = (f"{field[0]}_x", field[1]), (f"{field[0]}_y", field[1])
            else:
                field_sample, field_correct = f"{field}_x", f"{field}_y"
            match_field = field in table.columns and field in correct_answer.columns
            match_all = match_all and match_field
            record_match(
                correct=match_field,
                expected=field,
                picked=str(list(table.columns)),
                file_name=sample.file_name,
                jobtype="match_field"
            )
            if match_field:
                match_number = table[field].shape[0] == correct_answer[field].shape[0]
                match_all = match_all and match_number
                record_match(
                    correct=match_number,
                    expected=correct_answer[field].shape[0],
                    picked=table[field].shape[0],
                    file_name=sample.file_name,
                    jobtype="match_number"
                )

                for sample_value, correct_value in zip(comparison_df[field_sample], comparison_df[field_correct]):
                    match_value = fuzzy_compare(str(sample_value), str(correct_value))
                    match_all = match_all and match_value
                    record_match(
                        correct=match_value,
                        expected=correct_value,
                        picked=sample_value,
                        file_name=sample.file_name,
                        jobtype=field if type(field) == str else field[0]
                    )
        record_match(
            prompt=prompt,
            correct=match_all,
            expected=correct_str,
            picked=picked_str,
            file_name=sample.file_name,
            jobtype="match_all"
        )

    def run(self, recorder: RecorderBase):
        raw_samples = get_rag_dataset(self._prefix_registry_path(self.samples_jsonl).as_posix())
        for raw_sample in raw_samples:
            raw_sample["compare_fields"] = [field if type(field) == str else tuple(field) for field in
                                            raw_sample["compare_fields"]]

        samples = [FileSample(**raw_sample) for raw_sample in raw_samples]
        self.eval_all_samples(recorder, samples)
        return {
            "accuracy": evals.metrics.get_accuracy(recorder.get_events("match")),
        }


def tableMatchingStrict(df_ref, df_prompt, idx_col='Nickname'):
    df_ref = df_ref.set_index(idx_col)
    if len(df_prompt) == 0:
        return 0.0, 0.0
    df_prompt = df_prompt.set_index(idx_col)
    ref_columns = [col for col in df_ref.columns if col not in [idx_col]]
    idx_list = df_ref.index.values.tolist()
    prompt_idx_list = df_prompt.index.values.tolist()
    N, M = len(idx_list), len(ref_columns)
    match_score, total_match_score = 0.0, 0.0
    for idx in idx_list:
        _total_matching = 1.0
        for col in ref_columns:
            gt = df_ref.loc[idx, col]
            try:
                pd = df_prompt.loc[idx, col]
            except:
                pd = 'not found'
            _is_matching = fuzzy_compare(gt, pd)
            _total_matching *= float(_is_matching)
            match_score += float(_is_matching) / M
        total_match_score += _total_matching
        _total_matching = 1.0
    recall = max(len([item for item in prompt_idx_list if item in idx_list]) / len(idx_list), 1.0)
    print(f'Recall:{recall}, Acc: {match_score / N * recall}, Strict Acc: {total_match_score / N * recall}')
    return match_score / N * recall, total_match_score / N * recall
