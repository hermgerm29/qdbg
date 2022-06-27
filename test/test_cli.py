import io
import subprocess
import unittest

from contextlib import redirect_stdout
from unittest import mock

from qdbg import main
from qdbg import parse_traceback
from qdbg import QdbgError


class CliTests(unittest.TestCase):
    """
    Tests associated with the main cli interface
    """

    @mock.patch("subprocess.run")
    def test_successful_proc(self, mock_proc: mock.MagicMock) -> None:
        """Test that a successful process runs"""
        mock_proc.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"mock stdout"
        )

        with redirect_stdout(io.StringIO()) as f:
            main(args=["cmd"])

        self.assertEqual(f.getvalue(), "mock stdout\n")

    @mock.patch("subprocess.run")
    def test_file_not_found_proc(self, mock_proc: mock.MagicMock) -> None:
        """Test that file not found error is caught"""
        mock_proc.side_effect = FileNotFoundError
        with self.assertRaises(SystemExit):
            main(args=["cmd"])

    @mock.patch("subprocess.run")
    def test_no_commands(self, mock_proc: mock.MagicMock) -> None:
        """Test that an exception is raised if no command is provided"""
        with self.assertRaises(QdbgError):
            main(args=[])

    @mock.patch("webbrowser.open_new_tab")
    @mock.patch("subprocess.run")
    def test_webbrowser_not_found(
        self, mock_open_new_tab: mock.MagicMock, mock_proc: mock.MagicMock
    ) -> None:
        """Test for when no browser is found"""
        mock_proc.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stderr=b""
        )

        # Result if webbrowser.open_new_tab fails
        mock_open_new_tab.return_value = False

        with self.assertRaises(QdbgError):
            main(args=["cmd"])

    @mock.patch("webbrowser.open_new_tab")
    @mock.patch("qdbg.cli.get_search_url")
    @mock.patch("subprocess.run")
    def test_unsuccessful_proc(
        self,
        mock_proc: mock.MagicMock,
        mock_get_search_url: mock.MagicMock,
        mock_open_new_tab: mock.MagicMock,
    ) -> None:
        """Test process that fails and a browser tab is expected to open"""
        mock_proc.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stderr=b""
        )
        mock_get_search_url.return_value = "search_url"

        with self.assertRaises(SystemExit):
            main(args=["cmd"])

        mock_open_new_tab.assert_called_once_with(url="search_url")


class CliUtilsTest(unittest.TestCase):
    """
    Tests associated with cli utilities
    """

    trace = "line1\nline2\nline3"

    def test_parse_traceback_empty_str(self) -> None:
        """Test with empty string arg"""
        self.assertEqual(parse_traceback(stderr=""), "")

    def test_parse_traceback(self) -> None:
        """Expect to get the last line of the trace"""
        self.assertEqual(parse_traceback(stderr=self.trace), "line3")

    def test_parse_traceback_reversed(self) -> None:
        """Expect to get the first line of the trace"""
        self.assertEqual(parse_traceback(stderr=self.trace, from_bottom=False), "line1")
