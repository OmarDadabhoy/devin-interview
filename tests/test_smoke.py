import devin_interview
from devin_interview import greet


def test_package_imports():
    assert devin_interview.__version__ == "0.1.0"


def test_greet_default():
    assert greet() == "Hello, World!"


def test_greet_custom():
    assert greet("Devin") == "Hello, Devin!"
