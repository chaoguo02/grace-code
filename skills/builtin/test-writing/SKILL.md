---
name: Test Writing
description: 测试编写指南，包括单测结构、mock 策略、边界用例和 pytest 最佳实践
triggers:
  - test
  - 测试
  - 补测试
  - write test
  - unit test
---

## Test Structure (Arrange-Act-Assert)

```python
def test_user_creation_with_valid_email():
    # Arrange — set up preconditions
    email = "user@example.com"
    
    # Act — execute the behavior under test
    user = create_user(email=email)
    
    # Assert — verify the outcome
    assert user.email == email
    assert user.id is not None
```

## What to Test

### Priority Order
1. **Happy path** — normal expected behavior
2. **Edge cases** — empty input, single element, boundary values
3. **Error cases** — invalid input, missing resources, network failures
4. **Integration points** — boundaries between your code and external systems

### Edge Cases Checklist
| Input type | Edge cases |
|-----------|------------|
| String | `""`, `" "`, very long, unicode, special chars |
| Number | `0`, `-1`, `MAX_INT`, `float("inf")`, `NaN` |
| List | `[]`, `[single]`, duplicates, already sorted, reverse sorted |
| Dict | `{}`, missing keys, extra keys, None values |
| File | missing, empty, permissions error, very large |
| Network | timeout, 404, 500, malformed response |

## pytest Best Practices

### Naming
```python
# Pattern: test_{what}_{condition}_{expected}
def test_parse_email_with_plus_sign_extracts_local_part():
def test_login_with_expired_token_returns_401():
def test_cache_after_ttl_expires_returns_fresh_value():
```

### Fixtures
```python
@pytest.fixture
def db_session():
    """Create a test database session with rollback."""
    session = create_test_session()
    yield session
    session.rollback()

@pytest.fixture
def sample_user(db_session):
    """Pre-created user for tests that need an existing user."""
    return create_user(db_session, email="test@example.com")
```

### Parametrize for Multiple Cases
```python
@pytest.mark.parametrize("input,expected", [
    ("hello", "HELLO"),
    ("", ""),
    ("123", "123"),
    ("Hello World", "HELLO WORLD"),
])
def test_uppercase(input, expected):
    assert uppercase(input) == expected
```

## Mock Strategy

### When to Mock
- External services (APIs, databases, file system)
- Time-dependent behavior (`datetime.now()`)
- Random/non-deterministic operations
- Expensive operations (network calls, heavy computation)

### When NOT to Mock
- Your own code (test the real thing)
- Simple data transformations
- Value objects and pure functions

### Mock Patterns (pytest + unittest.mock)
```python
from unittest.mock import patch, MagicMock

# Patch at the usage site, not the definition site
@patch("mymodule.requests.get")
def test_fetch_data(mock_get):
    mock_get.return_value.json.return_value = {"key": "value"}
    result = fetch_data("http://api.example.com")
    assert result == {"key": "value"}
    mock_get.assert_called_once_with("http://api.example.com")

# Use spec to catch API misuse
mock_db = MagicMock(spec=DatabaseClient)
```

## Test Organization

```
tests/
├── conftest.py          # Shared fixtures
├── test_auth.py         # Group by module/feature
├── test_parser.py
└── integration/
    └── test_api.py      # Separate slow integration tests
```

## Anti-Patterns to Avoid
- Testing implementation details (private methods, internal state)
- Tests that depend on execution order
- Asserting too many things in one test
- Tests that pass when the feature is broken (false positive)
- Mocking everything (testing the mocks, not the code)

### For: $ARGUMENTS
Write tests for the code/feature described above following these guidelines.