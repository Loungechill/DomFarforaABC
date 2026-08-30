import tempfile
import unittest
from pathlib import Path

from filter_feed import (
    custom_label_for_purchases,
    load_purchase_stats,
    normalize_name,
    parse_purchases,
)


class CustomLabelTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(custom_label_for_purchases(0), "3")
        self.assertEqual(custom_label_for_purchases(1), "2")
        self.assertEqual(custom_label_for_purchases(2), "2")
        self.assertEqual(custom_label_for_purchases(3), "1")


class NameNormalizationTests(unittest.TestCase):
    def test_normalizes_case_spacing_unicode_and_html(self):
        self.assertEqual(
            normalize_name("  Чашка\u00a0&amp; БЛЮДЦЕ  "),
            normalize_name("чашка & блюдце"),
        )


class PurchaseCsvTests(unittest.TestCase):
    def write_csv(self, contents: str) -> Path:
        temp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        path = Path(temp.name)
        temp.close()
        path.write_text(contents, encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_aggregates_duplicate_product_names(self):
        path = self.write_csv(
            "Название товара или каталога,Расход,Конверсии,ABC\n"
            "Тестовый товар с достаточно длинным названием,100,1,B\n"
            "ТЕСТОВЫЙ   ТОВАР С ДОСТАТОЧНО ДЛИННЫМ НАЗВАНИЕМ,50,1,B\n"
            "Тестовый товар с достаточно длинным названием,0,0,C\n"
        )

        purchases, stats = load_purchase_stats(path)

        self.assertEqual(
            purchases[normalize_name("Тестовый товар с достаточно длинным названием")],
            2,
        )
        self.assertEqual(stats["purchase_csv_duplicate_rows_aggregated"], 2)
        self.assertEqual(stats["purchase_csv_total_conversions"], 2)

    def test_accepts_purchases_header_alias_and_decimal_integer(self):
        path = self.write_csv(
            "Название товара,Расход,Покупки\n"
            "Тестовый товар с достаточно длинным названием,100,2.0\n"
        )

        purchases, _ = load_purchase_stats(path)

        self.assertEqual(next(iter(purchases.values())), 2)

    def test_handles_quoted_commas_and_doubled_quotes(self):
        path = self.write_csv(
            'Название товара или каталога,"Расход, ₽",Конверсии,ABC\n'
            '"Вилка Robbe&Berking ""Мартеле"", 21,6 см","187,98",0,C\n'
        )

        purchases, _ = load_purchase_stats(path)

        self.assertEqual(next(iter(purchases.values())), 0)

    def test_rejects_fractional_and_negative_values(self):
        with self.assertRaisesRegex(RuntimeError, "non-negative integer"):
            parse_purchases("1.5", 2)
        with self.assertRaisesRegex(RuntimeError, "non-negative integer"):
            parse_purchases("-1", 2)


if __name__ == "__main__":
    unittest.main()

