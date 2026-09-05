"""Spreadsheet-safe CSV writers for member-controlled export text."""

from __future__ import annotations

import csv


def safe_cell(value):
    if isinstance(value, str):
        candidate = value.lstrip().lstrip("\ufeff").lstrip()
        if candidate.startswith(("=", "+", "-", "@")) or value.startswith(
            ("\t", "\r", "\n")
        ):
            return "'" + value
    return value


class SafeCSVWriter:
    def __init__(self, output, *args, **kwargs):
        self._writer = csv.writer(output, *args, **kwargs)

    def writerow(self, row):
        return self._writer.writerow(safe_cell(value) for value in row)

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


class SafeCSVDictWriter(csv.DictWriter):
    def __init__(self, output, fieldnames, *args, **kwargs):
        super().__init__(output, fieldnames, *args, **kwargs)
        self.writer = SafeCSVWriter(output, dialect=self.writer.dialect)
