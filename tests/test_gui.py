"""GUI-focused tests — dependency-light, no Streamlit/Plotly required."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from gui import charts, components, pages


class TestComponentsWithoutStreamlit(unittest.TestCase):
    def test_model_selector_returns_none(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            result = components.model_selector([])
            self.assertIsNone(result)

    def test_text_input_returns_default(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            result = components.text_input("label")
            self.assertEqual(result, "")

    def test_text_area_returns_default(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            result = components.text_area("label")
            self.assertEqual(result, "")

    def test_info_noop(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            components.info("test")

    def test_warning_noop(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            components.warning("test")

    def test_error_noop(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            components.error("test")

    def test_success_noop(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            components.success("test")

    def test_write_json_noop(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            components.write_json({"key": "value"})

    def test_data_table_noop(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            components.data_table([])

    def test_spinner_returns_none(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            result = components.spinner("test")
            self.assertIsNone(result)

    def test_checkbox_returns_default(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            result = components.checkbox("label")
            self.assertFalse(result)

    def test_button_returns_false(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            result = components.button("label")
            self.assertFalse(result)

    def test_slider_returns_default(self) -> None:
        with patch.object(components, "_streamlit_available", return_value=False):
            result = components.slider("label")
            self.assertEqual(result, 0.0)


class TestPagesReturnWithoutStreamlit(unittest.TestCase):
    def test_render_catalog_page(self) -> None:
        with patch.object(pages, "_streamlit_available", return_value=False):
            pages.render_catalog_page(models=[], stats={})

    def test_render_chat_page(self) -> None:
        with patch.object(pages, "_streamlit_available", return_value=False):
            pages.render_chat_page(models=[], experiment_ids=[])

    def test_render_dashboard_page(self) -> None:
        with patch.object(pages, "_streamlit_available", return_value=False):
            pages.render_dashboard_page(experiment_ids=[])


class TestChartsHelpers(unittest.TestCase):
    def test_phase_timeline_chart(self) -> None:
        sessions = [
            {"phase": "baseline"},
            {"phase": "poison"},
            {"phase": "baseline"},
            {"phase": "probe"},
        ]
        result = charts.phase_timeline_chart(sessions)
        self.assertEqual(result, {"baseline": 2, "poison": 1, "probe": 1})

    def test_phase_timeline_chart_empty(self) -> None:
        result = charts.phase_timeline_chart([])
        self.assertEqual(result, {})

    def test_model_comparison_data(self) -> None:
        experiments = [
            {"id": "exp1", "model_id": "groq/test", "title": "Test", "status": "completed",
             "session_count": 2, "turn_count": 10},
        ]
        result = charts.model_comparison_data(experiments)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["experiment_id"], "exp1")
        self.assertEqual(result[0]["sessions"], 2)

    def test_model_comparison_data_empty(self) -> None:
        result = charts.model_comparison_data([])
        self.assertEqual(result, [])

    def test_bar_chart_returns_none_without_plotly(self) -> None:
        with patch.object(charts, "_plotly_available", return_value=False):
            result = charts.bar_chart([], "x", "y")
            self.assertIsNone(result)

    def test_line_chart_returns_none_without_plotly(self) -> None:
        with patch.object(charts, "_plotly_available", return_value=False):
            result = charts.line_chart([], "x", "y")
            self.assertIsNone(result)

    def test_scatter_chart_returns_none_without_plotly(self) -> None:
        with patch.object(charts, "_plotly_available", return_value=False):
            result = charts.scatter_chart([], "x", "y")
            self.assertIsNone(result)

    def test_pie_chart_returns_none_without_plotly(self) -> None:
        with patch.object(charts, "_plotly_available", return_value=False):
            result = charts.pie_chart([], "names", "values")
            self.assertIsNone(result)


class TestAppFunctions(unittest.TestCase):
    def test_main_is_callable(self) -> None:
        import app as app_module
        self.assertTrue(callable(app_module.main))

    def test_streamlit_check_function(self) -> None:
        import app as app_module
        result = app_module._streamlit_available()
        self.assertIsInstance(result, bool)


if __name__ == "__main__":
    unittest.main()
