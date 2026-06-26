# Tiếng Việt và Unicode

openpyxl và pandas lưu `.xlsx` theo Office Open XML (UTF-8). Tiếng Việt có dấu được hỗ trợ mà không cần cấu hình thêm.

## Ghi CSV có tiếng Việt

Excel trên Windows mặc định mở CSV với ANSI (cp1252), không đọc UTF-8 thuần.

```python
# ✅ UTF-8 with BOM — Excel tự nhận biết
df.to_csv('output.csv', index=False, encoding='utf-8-sig')

# ❌ UTF-8 không BOM — Excel hiển thị sai dấu
df.to_csv('output.csv', index=False, encoding='utf-8')
```

## Ghi XLSX có tiếng Việt

```python
from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws['A1'] = 'Hợp đồng'
ws.append(['Nguyễn Văn A', 'Phòng Kế toán', 1_000_000])
wb.save('output.xlsx')  # Không cần chỉ định encoding
```

## Đọc CSV tiếng Việt

```python
import pandas as pd

try:
    df = pd.read_csv('data.csv', encoding='utf-8-sig')
except UnicodeDecodeError:
    df = pd.read_csv('data.csv', encoding='cp1258')
```

## Font hiển thị

```python
from openpyxl.styles import Font

ws['A1'].font = Font(name='Times New Roman', bold=True, size=12)  # hoặc Arial, Calibri
```

| Font | Hỗ trợ tiếng Việt |
|------|-------------------|
| Arial | ✅ |
| Times New Roman | ✅ |
| Calibri | ✅ |
| Courier New | ✅ |
