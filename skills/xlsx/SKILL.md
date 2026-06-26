---
name: xlsx-spreadsheet-operations
description: "Use when spreadsheet files (.xlsx, .xlsm, .csv, .tsv) are the primary input or output. Trigger when the user names or paths a spreadsheet file, wants tabular data created or edited, needs messy tabular data cleaned into a spreadsheet, or asks for financial models in Excel. Do NOT trigger when the deliverable is a Word document, HTML report, standalone Python script, database pipeline, or Google Sheets API."
license: Proprietary. LICENSE.txt has complete terms
---

# XLSX Spreadsheet Operations

## Overview

**Core principle:** Use Excel formulas in the workbook—not Python-calculated hardcoded values—so spreadsheets stay dynamic when source data changes.

LibreOffice recalculates formula values via `scripts/recalc.py` (auto-configures on first run; sandbox Unix sockets handled by `scripts/office/soffice.py`).

## When to Use

```dot
digraph when_xlsx {
    "User mentions .xlsx/.csv/.tsv or tabular deliverable?" [shape=diamond];
    "Primary output is a spreadsheet file?" [shape=diamond];
    "Use xlsx skill" [shape=box];
    "Use another skill" [shape=box];

    "User mentions .xlsx/.csv/.tsv or tabular deliverable?" -> "Primary output is a spreadsheet file?" [label="yes"];
    "User mentions .xlsx/.csv/.tsv or tabular deliverable?" -> "Use another skill" [label="no"];
    "Primary output is a spreadsheet file?" -> "Use xlsx skill" [label="yes"];
    "Primary output is a spreadsheet file?" -> "Use another skill" [label="no"];
}
```

**Use when:** reading/editing/creating `.xlsx`, `.xlsm`, `.csv`, `.tsv`; cleaning malformed tabular data; financial models.

**Do not use when:** Word/HTML deliverable, Google Sheets API, or script-only output with no spreadsheet file.

## Quick Reference

| Task | Tool | Example |
|------|------|---------|
| Read/analyze data | pandas | `pd.read_excel('file.xlsx')` |
| Write simple data | pandas | `df.to_excel('out.xlsx', index=False)` |
| Formulas + formatting | openpyxl | `load_workbook` / `Workbook()` |
| Recalculate formulas | recalc.py | `python scripts/recalc.py out.xlsx` |
| Unpack/pack OOXML | office scripts | `unpack.py` / `pack.py` |
| Validate structure | validate.py | `python scripts/office/validate.py file.xlsx` |
| Financial model rules | [financial-models.md](financial-models.md) | Color, formats, assumptions |
| Vietnamese text/CSV | [vietnamese-unicode.md](vietnamese-unicode.md) | `utf-8-sig`, fonts |

## Common Mistakes

| Mistake | Consequence | Fix |
|---------|-------------|-----|
| Hardcode Python totals in cells | Sheet won't update with data | `sheet['B10'] = '=SUM(B2:B9)'` |
| Save after `data_only=True` | Formulas permanently lost | Read with `data_only=True`; save only from `data_only=False` |
| Skip `recalc.py` after formulas | Cached values missing/stale | Run `python scripts/recalc.py file.xlsx` |
| CSV UTF-8 without BOM on Windows | Vietnamese garbled in Excel | `encoding='utf-8-sig'` |
| Wrong row when mapping from pandas | Off-by-one references | DataFrame row 5 = Excel row 6 |

## Output Requirements

### All Excel files
- Professional font (Arial, Times New Roman) unless user/template says otherwise
- **Zero formula errors** (#REF!, #DIV/0!, #VALUE!, #N/A, #NAME?)
- When editing templates: match existing format exactly; template rules override defaults

### Financial models
See **[financial-models.md](financial-models.md)** for color coding, number formats, and assumption documentation.

## CRITICAL: Use Formulas, Not Hardcoded Values

```python
# ❌ Bad
sheet['B10'] = df['Sales'].sum()

# ✅ Good
sheet['B10'] = '=SUM(B2:B9)'
```

Applies to totals, percentages, ratios, and all derived values.

## Workflow

1. **Choose tool:** pandas (data) or openpyxl (formulas/formatting)
2. **Create/load** workbook
3. **Modify** data, formulas, formatting
4. **Save**
5. **Recalculate** (mandatory if formulas used):
   ```bash
   python scripts/recalc.py output.xlsx
   ```
6. **Fix errors** from JSON `error_summary` (#REF!, #DIV/0!, etc.) and recalc again

### pandas (read/analyze)

```python
import pandas as pd

df = pd.read_excel('file.xlsx')
all_sheets = pd.read_excel('file.xlsx', sheet_name=None)
df.to_excel('output.xlsx', index=False)
```

### openpyxl (create/edit)

```python
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

wb = Workbook()
sheet = wb.active
sheet['A1'] = 'Hello'
sheet['B2'] = '=SUM(A1:A10)'
sheet['A1'].font = Font(bold=True)
wb.save('output.xlsx')

wb = load_workbook('existing.xlsx')
wb.active['A1'] = 'New Value'
wb.save('modified.xlsx')
```

## Recalculating formulas

openpyxl stores formulas as strings without evaluated values.

```bash
python scripts/recalc.py <excel_file> [timeout_seconds]
```

Returns JSON: `status`, `total_errors`, `total_formulas`, `error_summary` (locations per error type).

Works on **Windows, Linux, and macOS** when LibreOffice is installed.

## Formula verification

- Test 2–3 references before scaling formulas
- Excel columns are 1-indexed (DataFrame row 5 → Excel row 6)
- Check `pd.notna()` for nulls; guard denominators for #DIV/0!
- Cross-sheet refs: `Sheet1!A1`

## Best practices

| Library | Best for |
|---------|----------|
| pandas | Analysis, bulk read/write, typing (`dtype`, `usecols`, `parse_dates`) |
| openpyxl | Formulas, formatting, sheet ops |

- Cell indices are **1-based** in openpyxl
- `read_only=True` / `write_only=True` for large files
- Minimal Python code; document assumptions in **cell comments** on the workbook

## Vietnamese / Unicode

See **[vietnamese-unicode.md](vietnamese-unicode.md)** for CSV BOM, reading cp1258, and fonts.
