import unittest

from processing import classify, redact


class ProcessingTest(unittest.TestCase):
    def test_redacts_all_requested_types(self):
        values = [
            ("NAMA", "Nama: Budi Contoh", "Budi Contoh"),
            ("EMAIL", "budi@example.test", "budi@example.test"),
            ("PHONE", "+62 812-3456-7890", "+62 812-3456-7890"),
            ("NIK", "NIK: 3201010101010001", "3201010101010001"),
            ("NPWP", "01.234.567.8-901.234", "01.234.567.8-901.234"),
            ("KARTU", "4111 1111 1111 1111", "4111 1111 1111 1111"),
        ]
        text, findings = redact("Invoice faktur\n" + "\n".join(item[1] for item in values))
        self.assertEqual({item[0] for item in values}, {item[0] for item in findings})
        for kind, _, raw in values:
            self.assertIn(f"[{kind}]", text)
            self.assertNotIn(raw, text)
        self.assertEqual(classify(text, findings), ("financial", "high"))

    def test_email_with_spacing_inserted_by_ocr(self):
        text, findings = redact("Email: demo@ example.test")
        self.assertEqual(text, "Email: [EMAIL]")
        self.assertEqual(findings, [("EMAIL", .99, 1)])

    def test_classification(self):
        for text, category, risk in [
            ("kontrak pasal perjanjian", "legal", "medium"),
            ("rekrutmen pegawai gaji", "hr", "medium"),
            ("deployment server database", "technical", "low"),
            ("catatan harian", "personal", "low"),
        ]:
            with self.subTest(category=category):
                self.assertEqual(classify(text, []), (category, risk))


if __name__ == "__main__":
    unittest.main()
