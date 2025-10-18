import sqlite3
import unittest
from unittest.mock import patch
import uuid
import os
import database

class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.db_file = "test_bot.db"
        database.DATABASE_FILE = self.db_file
        if os.path.exists(self.db_file):
            os.remove(self.db_file)
        self.con = sqlite3.connect(self.db_file)
        self.con.execute("""
            CREATE TABLE redeem_codes (
                code TEXT PRIMARY KEY NOT NULL,
                duration_days INTEGER NOT NULL,
                used_by_user_id INTEGER,
                used_at TEXT
            )
        """)
        self.con.commit()
        # Mock logger to avoid polluting test output
        patcher = patch('database.logger')
        self.mock_logger = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.con.close()
        if os.path.exists(self.db_file):
            os.remove(self.db_file)

    @patch('uuid.uuid4')
    def test_generate_redeem_codes_handles_collisions(self, mock_uuid4):
        # Mock uuid4 to return specific UUIDs to control the generated codes
        mock_uuid4.side_effect = [
            uuid.UUID('ABCDEF12-0000-0000-0000-000000000000'),
            uuid.UUID('ABCDEF12-0000-0000-0000-000000000000'), # This will cause a collision
            uuid.UUID('12345678-0000-0000-0000-000000000000')
        ]

        codes = database.generate_redeem_codes(2, 30)

        # We expect 2 codes to be generated successfully
        self.assertEqual(len(codes), 2)
        self.assertIn('ABCDEF12', codes)
        self.assertIn('12345678', codes)

        # Check that uuid.uuid4 was called 3 times (1 success, 1 collision, 1 success)
        self.assertEqual(mock_uuid4.call_count, 3)

        # Verify that the correct codes are actually in the database
        cur = self.con.cursor()
        res = cur.execute("SELECT code FROM redeem_codes").fetchall()
        retrieved_codes = [row[0] for row in res]

        self.assertEqual(len(retrieved_codes), 2)
        self.assertIn('ABCDEF12', retrieved_codes)
        self.assertIn('12345678', retrieved_codes)

if __name__ == '__main__':
    unittest.main()