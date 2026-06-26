# Tiếng Việt và Unicode trong PDF

## Vấn đề với font mặc định

> **CẢNH BÁO**: Font mặc định của reportlab (Helvetica, Times-Roman, Courier) **không hỗ trợ tiếng Việt**. Ký tự có dấu sẽ hiển thị thành ô vuông hoặc không xuất hiện.

**Bắt buộc dùng TTFont** với font Unicode khi tạo PDF tiếng Việt.

## Cài font và đăng ký

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os

# Cách 1: Dùng font có sẵn trên Windows
font_paths = [
    r"C:\Windows\Fonts\arial.ttf",      # Arial
    r"C:\Windows\Fonts\times.ttf",      # Times New Roman
    r"C:\Windows\Fonts\calibri.ttf",    # Calibri
]

# Cách 2: Dùng DejaVu (có sẵn trên Linux/Mac, hoặc tải về)
# pip install reportlab  # đi kèm một số font

# Đăng ký font
pdfmetrics.registerFont(TTFont('Arial', r'C:\Windows\Fonts\arial.ttf'))
pdfmetrics.registerFont(TTFont('Arial-Bold', r'C:\Windows\Fonts\arialbd.ttf'))
pdfmetrics.registerFont(TTFont('Arial-Italic', r'C:\Windows\Fonts\ariali.ttf'))
pdfmetrics.registerFont(TTFont('TimesNewRoman', r'C:\Windows\Fonts\times.ttf'))

# Đăng ký bộ font để dùng bold/italic tự động
from reportlab.pdfbase.pdfmetrics import registerFontFamily
registerFontFamily('Arial',
    normal='Arial',
    bold='Arial-Bold',
    italic='Arial-Italic'
)
```

## Tạo PDF tiếng Việt với Canvas

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

pdfmetrics.registerFont(TTFont('Arial', r'C:\Windows\Fonts\arial.ttf'))
pdfmetrics.registerFont(TTFont('Arial-Bold', r'C:\Windows\Fonts\arialbd.ttf'))

c = canvas.Canvas("output_viet.pdf", pagesize=A4)
width, height = A4

# Dùng font đã đăng ký — không dùng 'Helvetica'
c.setFont('Arial-Bold', 16)
c.drawString(50, height - 80, "HỢP ĐỒNG LAO ĐỘNG")

c.setFont('Arial', 12)
c.drawString(50, height - 120, "Người lao động: Nguyễn Văn A")
c.drawString(50, height - 140, "Chức vụ: Nhân viên kế toán")
c.drawString(50, height - 160, "Mức lương: 15.000.000 VNĐ/tháng")

c.save()
```

## Tạo PDF tiếng Việt với Platypus (flowable)

```python
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

pdfmetrics.registerFont(TTFont('Arial', r'C:\Windows\Fonts\arial.ttf'))
pdfmetrics.registerFont(TTFont('Arial-Bold', r'C:\Windows\Fonts\arialbd.ttf'))

doc = SimpleDocTemplate("report_viet.pdf", pagesize=A4,
                        leftMargin=50, rightMargin=50,
                        topMargin=50, bottomMargin=50)

# Tạo styles dùng font Unicode
style_title = ParagraphStyle(
    'ViTitle',
    fontName='Arial-Bold',
    fontSize=16,
    leading=20,
    spaceAfter=12,
)
style_body = ParagraphStyle(
    'ViBody',
    fontName='Arial',
    fontSize=12,
    leading=16,
    spaceAfter=8,
)

story = [
    Paragraph("BÁO CÁO TÀI CHÍNH QUÝ I/2025", style_title),
    Spacer(1, 12),
    Paragraph("Kết quả hoạt động kinh doanh quý I năm 2025 đạt mức tăng trưởng ấn tượng.", style_body),
    Paragraph("Doanh thu thuần: 250 tỷ VNĐ (tăng 18% so với cùng kỳ)", style_body),
]

doc.build(story)
```

## Khung hỗ trợ cross-platform (Windows + Linux)

```python
import os
import sys
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

def register_viet_fonts():
    """Tìm và đăng ký font Arial hỗ trợ tiếng Việt cho mọi OS."""
    candidates = {
        'Arial': [
            r'C:\Windows\Fonts\arial.ttf',
            '/usr/share/fonts/truetype/msttcorefonts/Arial.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
        ],
        'Arial-Bold': [
            r'C:\Windows\Fonts\arialbd.ttf',
            '/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        ],
    }
    for name, paths in candidates.items():
        for path in paths:
            if os.path.exists(path):
                pdfmetrics.registerFont(TTFont(name, path))
                break
        else:
            raise FileNotFoundError(
                f"Không tìm thấy font '{name}'. Cài bằng: "
                "sudo apt install fonts-liberation  # Linux\n"
                "hoặc: pip install reportlab[all]  # bao gồm Bitstream Vera"
            )
    return 'Arial', 'Arial-Bold'

normal_font, bold_font = register_viet_fonts()
```

## Tóm tắt các lỗi thường gặp

| Lỗi | Nguyên nhân | Giải pháp |
|------|--------------|------------|
| Ký tự hiển thị ô vuông | Dùng font không hỗ trợ Unicode | Đăng ký TTFont |
| `KeyError: 'glyph ...'` | Glyph không có trong font | Chọn font đầy đủ Unicode |
| Ký tự biến mất | Font không embed trong PDF | Dùng `TTFont` (đã embed mặc định) |
| In sai dấu | Encoding không phù hợp UTF-8 | Đảm bảo script Python lưu UTF-8 |
