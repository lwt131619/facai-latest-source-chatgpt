"""sqlmap driver tests: the parser must never turn a negative verdict positive."""
import pytest

from facai import sqlmap_driver


NEGATIVE_OUTPUT = """[03:29:30] [WARNING] GET parameter 'cat' does not seem to be injectable
[03:29:30] [ERROR] all tested parameters do not appear to be injectable. Try to increase values for '--level'/'--risk' options
[03:29:30] [WARNING] HTTP error codes detected during run:
403 (Forbidden) - 444 times
[*] ending @ 03:29:30 /2026-10-28/
"""

POSITIVE_OUTPUT = """sqlmap identified the following injection point(s) with a total of 42 HTTP(s) requests:
---
Parameter: cat (GET)
    Type: boolean-based blind
    Title: AND boolean-based blind - WHERE or HAVING clause
    Payload: cat=1 AND 2315=2315
---
[03:29:31] [INFO] the back-end DBMS is MySQL
back-end DBMS: MySQL >= 5.0
[03:29:32] [INFO] fetching banner
banner: '5.5.62-MariaDB'
[03:29:33] [INFO] fetching current database
current database (equivalent to schema on PostgreSQL): 'acuart'
[03:29:33] [INFO] fetching current user
current user: 'root@localhost'
available databases [3]:
[*] acuart
[*] information_schema
[*] mysql
Database: acuart
Table: users
[3 entries]
+----+--------+----------+
| id | name   | pass     |
+----+--------+----------+
| 1  | admin  | secret   |
| 2  | user   | pw       |
+----+--------+----------+
"""

PARTIAL_OUTPUT = """[INFO] testing 'MySQL >= 5.0 error-based - Parameter replace (FLOOR)'
[WARNING] GET parameter 'cat' does not seem to be injectable
"""


def test_negative_verdict_is_not_confirmed():
    parsed = sqlmap_driver._parse(NEGATIVE_OUTPUT)
    assert parsed['injectable'] is False
    assert parsed['techniques'] == []


def test_positive_verdict_carries_enumeration():
    parsed = sqlmap_driver._parse(POSITIVE_OUTPUT)
    assert parsed['injectable'] is True
    assert 'MySQL' in (parsed['dbms'] or '')
    assert 'MariaDB' in (parsed['banner'] or '')
    assert parsed['current_db'] and parsed['current_user']
    assert 'boolean-based blind' in ' '.join(parsed['techniques'])
    assert 'acuart' in parsed['databases']
    assert parsed['dumps'] and parsed['dumps'][0]['table'] == 'users'
    assert parsed['dumps'][0]['row_count'] >= 2


def test_partial_warning_output_stays_negative():
    parsed = sqlmap_driver._parse(PARTIAL_OUTPUT)
    assert parsed['injectable'] is False


def test_proxy_args_split_credentials():
    args = sqlmap_driver._proxy_arg('http://user:secret@isp.example.com:10001')
    proxy_index = args.index('--proxy')
    assert args[proxy_index + 1] == 'http://isp.example.com:10001'
    cred_index = args.index('--proxy-cred')
    assert args[cred_index + 1] == 'user:secret'
    assert sqlmap_driver._proxy_arg(None) == []


def test_target_build_keeps_params():
    target = sqlmap_driver._build_target('https://h.example/item?id=7', params=['id'])
    assert 'id=7' in target or 'id=1' in target


def test_unavailable_driver_reports_cleanly():
    result = sqlmap_driver.enumerate_injection('https://h.example/item?id=1')
    if sqlmap_driver.available():
        pytest.skip('sqlmap installed; unavailability path not reachable')
    assert result['ok'] is False and 'not installed' in result['error']
