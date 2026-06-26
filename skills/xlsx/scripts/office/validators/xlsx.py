"""
Validator for Excel spreadsheet XML files against XSD schemas.
"""

from .base import BaseSchemaValidator


class XLSXSchemaValidator(BaseSchemaValidator):

    SPREADSHEETML_NAMESPACE = (
        "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    )

    REQUIRED_PATHS = (
        "[Content_Types].xml",
        "xl/workbook.xml",
        "_rels/.rels",
    )

    def validate(self):
        if not self._validate_required_structure():
            return False

        if not self.validate_xml():
            return False

        all_valid = True
        for check in (
            self.validate_namespaces,
            self.validate_unique_ids,
            self.validate_file_references,
            self.validate_content_types,
            self.validate_against_xsd,
            self.validate_all_relationship_ids,
        ):
            if not check():
                all_valid = False

        return all_valid

    def _validate_required_structure(self):
        errors = []
        for rel_path in self.REQUIRED_PATHS:
            if not (self.unpacked_dir / rel_path).exists():
                errors.append(
                    f"  Missing required file: {rel_path}"
                )

        workbook = self.unpacked_dir / "xl" / "workbook.xml"
        if workbook.exists():
            worksheets = list((self.unpacked_dir / "xl" / "worksheets").glob("sheet*.xml"))
            if not worksheets:
                errors.append("  No worksheets found in xl/worksheets/")

        if errors:
            print(f"FAILED - Found {len(errors)} structural errors:")
            for error in errors:
                print(error)
            return False

        if self.verbose:
            print("PASSED - Required XLSX structure present")
        return True


if __name__ == "__main__":
    raise RuntimeError("This module should not be run directly.")
