import healops


def test_version_is_defined():
    assert isinstance(healops.__version__, str)
    assert healops.__version__