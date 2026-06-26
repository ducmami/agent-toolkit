# Financial Model Standards

Use when building or editing financial models in Excel. Existing template conventions ALWAYS override these guidelines.

## Color Coding Standards

Unless otherwise stated by the user or existing template:

| Color | RGB | Use |
|-------|-----|-----|
| Blue text | 0,0,255 | Hardcoded inputs, scenario numbers users will change |
| Black text | 0,0,0 | ALL formulas and calculations |
| Green text | 0,128,0 | Links to other worksheets in same workbook |
| Red text | 255,0,0 | External links to other files |
| Yellow background | 255,255,0 | Key assumptions needing attention |

## Number Formatting

- **Years**: Text strings (e.g., "2024" not "2,024")
- **Currency**: `$#,##0`; specify units in headers ("Revenue ($mm)")
- **Zeros**: `$#,##0;($#,##0);-` (display as "-")
- **Percentages**: 0.0% (one decimal)
- **Multiples**: 0.0x (EV/EBITDA, P/E)
- **Negatives**: Parentheses (123), not minus -123

## Formula Construction

### Assumptions Placement
- Place ALL assumptions in separate cells; reference them in formulas
- Example: `=B5*(1+$B$6)` not `=B5*1.05`

### Error Prevention
- Verify cell references and range boundaries
- Keep formulas consistent across projection periods
- Test edge cases (zero, negative values)
- Avoid unintended circular references

### Hardcode Documentation
Format beside cell or in comment: `Source: [System/Document], [Date], [Reference], [URL]`

Examples:
- `Source: Company 10-K, FY2024, Page 45, Revenue Note, [SEC EDGAR URL]`
- `Source: Bloomberg Terminal, 8/15/2025, AAPL US Equity`
