"""The CI test dashboard (tools/test_dashboard.py): reading reports, the page and the badges."""
import pytest

pytestmark = pytest.mark.unit

PYTEST_JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" tests="4">
<testcase classname="tests.unit.test_commands" name="test_valid_command[update]" time="0.01"/>
<testcase classname="tests.unit.test_commands" name="test_garbage_payload" time="0.02">
  <failure message="assert 1 == 2">long traceback</failure></testcase>
<testcase classname="tests.component.test_db" name="test_reconnects" time="0.5">
  <skipped message="OTA_TEST_DB is not set"/></testcase>
<testcase classname="tests.component.test_device" name="test_update &lt;ok&gt;" time="1.25"/>
</testsuite></testsuites>"""

E2E_JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="end-to-end demo">
<testcase classname="e2e.Step 02: Devices connect" name="device-001 connected" time="0"/>
<testcase classname="e2e.Step 04: Crash" name="crash reported" time="0">
  <failure message="NOT WORKING">no crash_count</failure></testcase>
</testsuite></testsuites>"""

COVERAGE_XML = """<?xml version="1.0" ?><coverage line-rate="0.75"><packages>
<package name="deploy.orchestrator"><classes><class filename="deploy/orchestrator/a.py"><lines>
  <line number="1" hits="1"/><line number="2" hits="0"/></lines></class></classes></package>
<package name="tools"><classes><class filename="tools/release.py"><lines>
  <line number="1" hits="3"/><line number="2" hits="1"/></lines></class></classes></package>
</packages></coverage>"""


@pytest.fixture(scope="module")
def dash():
    from conftest import load_service_module
    return load_service_module("tools", "test_dashboard")


@pytest.fixture
def reports(tmp_path):
    (tmp_path / "pytest.xml").write_text(PYTEST_JUNIT)
    (tmp_path / "e2e.xml").write_text(E2E_JUNIT)
    (tmp_path / "coverage.xml").write_text(COVERAGE_XML)
    (tmp_path / "htmlcov").mkdir()
    (tmp_path / "htmlcov" / "index.html").write_text("<html>coverage</html>")
    (tmp_path / ".coveragerc").write_text("[report]\nfail_under = 70\n")
    return tmp_path


def test_reads_pytest_results_by_layer_and_status(dash, reports):
    cases = dash.read_junit(reports / "pytest.xml")
    assert [(c.layer, c.group, c.name, c.status) for c in cases] == [
        ("unit", "test_commands.py", "valid command [update]", "passed"),
        ("unit", "test_commands.py", "garbage payload", "failed"),
        ("component", "test_db.py", "reconnects", "skipped"),
        ("component", "test_device.py", "update <ok>", "passed"),
    ]
    assert cases[1].message == "assert 1 == 2"


def test_reads_demo_checks_as_end_to_end(dash, reports):
    cases = dash.read_junit(reports / "e2e.xml")
    assert [(c.layer, c.group, c.status) for c in cases] == [
        ("e2e", "Step 02: Devices connect", "passed"), ("e2e", "Step 04: Crash", "failed")]


def test_coverage_per_component(dash, reports):
    total, rows = dash.read_coverage(reports / "coverage.xml")
    assert total == 0.75
    assert rows == [("Orchestrator", 1, 2), ("Release and CI tools", 2, 2)]


def test_builds_page_and_badges(dash, reports, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_SHA", "abcdef1234567")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    out = reports / "site"
    assert dash.main(["--junit", str(reports / "pytest.xml"), "--junit", str(reports / "e2e.xml"),
                      "--coverage", str(reports / "coverage.xml"), "--htmlcov", str(reports / "htmlcov"),
                      "--coveragerc", str(reports / ".coveragerc"), "--out", str(out)]) == 0
    page = (out / "index.html").read_text()
    assert "✘ 2 failing" in page
    assert "update &lt;ok&gt;" in page                        # names are escaped
    assert "1/2" in page                                       # end-to-end checks tile
    assert "minimum 70%" in page and 'href="coverage/index.html"' in page
    assert "https://github.com/owner/repo/actions/runs/42" in page and ">abcdef1<" in page
    assert (out / "coverage" / "index.html").is_file()
    assert "2 failed" in (out / "badges" / "tests.svg").read_text()
    assert "75%" in (out / "badges" / "coverage.svg").read_text()
    assert "1/2 checks" in (out / "badges" / "e2e.svg").read_text()


def test_missing_reports_still_publish_a_page(dash, tmp_path):
    out = tmp_path / "site"
    assert dash.main(["--junit", str(tmp_path / "absent.xml"), "--out", str(out),
                      "--coveragerc", str(tmp_path / "none")]) == 0
    assert "No test results" in (out / "index.html").read_text()
    assert "no data" in (out / "badges" / "coverage.svg").read_text()


def test_all_passing_is_green(dash, tmp_path):
    (tmp_path / "ok.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.unit.test_x" name="test_a" time="0"/>'
        "</testsuite></testsuites>")
    out = tmp_path / "site"
    dash.main(["--junit", str(tmp_path / "ok.xml"), "--out", str(out), "--coveragerc", str(tmp_path / "x")])
    assert "✔ All tests passing" in (out / "index.html").read_text()
    assert "1 passed" in (out / "badges" / "tests.svg").read_text()
