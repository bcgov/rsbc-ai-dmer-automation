def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: calls the real Azure OpenAI deployment (opt-in, DMER_LIVE_TESTS=1)",
    )
