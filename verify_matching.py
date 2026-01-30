import logging
import sys
from app.api_helper import match_and_update_accounts

# Setup logging
logging.basicConfig(level=logging.INFO)


class MockCollection:
    def __init__(self, data):
        self.data = data

    def find_one(self, query):
        if "account_number" in query and "$regex" in query["account_number"]:
            regex = query["account_number"]["$regex"]
            # remove .* and $
            suffix = regex.replace(".*", "").replace("$", "")

            for item in self.data:
                if item["account_number"].endswith(suffix):
                    return item
        return None


def test_matching():
    # Mock data
    accounts_db = [
        {"account_number": "943214321321200", "alias": "Account 200"},
        {"account_number": "123456789266", "alias": "Account 266"},
    ]

    mock_col = MockCollection(accounts_db)

    # Test case 1: Match found
    tv = {
        "originAccount": "XXXXXXXXX200",  # Should match
        "destinationAccount": "XXXXXXXXX999",  # Should NOT match
        "amount": 10,
    }

    print("Original TV:", tv)
    result = match_and_update_accounts(tv, mock_col)
    print("Updated TV:", result)

    assert result["originAccount"] == "943214321321200", (
        "Origin account should be updated"
    )
    assert result["destinationAccount"] == "XXXXXXXXX999", (
        "Destination account should NOT change"
    )

    print("\n✅ Verification PASSED")


if __name__ == "__main__":
    test_matching()
