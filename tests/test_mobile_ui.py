"""Static regression tests for the mobile workspace.

The functional backend tests run separately; these catch accidentally dropping
mobile navigation, touch-sized inputs or real-data chart handling.
"""
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app" / "static"


class NavParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tabs = []
    def handle_starttag(self, tag, attrs):
        if tag != "button":
            return
        a = dict(attrs)
        if "navitem" in a.get("class", "").split():
            self.tabs.append(a)


def test_mobile_navigation_preserves_all_five_sections():
    html = (ROOT / "index.html").read_text()
    parsed = NavParser()
    parsed.feed(html)
    assert [t["data-tab"] for t in parsed.tabs] == [
        "dashboard", "connections", "sync", "logs", "security"
    ]
    assert all(t.get("type") == "button" and t.get("aria-label") for t in parsed.tabs)
    assert html.count('class="nav-short"') == 5


def test_mobile_touch_layout_and_readable_forms():
    css = (ROOT / "style.css").read_text()
    assert ".nav-short{display:none}" in css
    assert "grid-template-columns:repeat(5,minmax(0,1fr))" in css
    assert "safe-area-inset-bottom" in css
    assert "min-height:48px;font-size:16px" in css
    assert "prefers-reduced-motion" in css


def test_chart_uses_actual_measurement_dates_not_sample_dates():
    script = (ROOT / "app.js").read_text()
    chart_section = script.split("function chart(rows){", 1)[1].split("function indicator(", 1)[0]
    assert "series.length<2" in chart_section
    assert "chart-empty" in chart_section
    assert "dateFormat.format(date)" in chart_section
    assert "weight_kg" in chart_section
    assert "series=[90" not in chart_section
