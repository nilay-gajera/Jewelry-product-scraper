from scrapy.settings import Settings, overridden_settings

import lgd_scraper.settings as project_settings


def test_aws_credentials_are_not_copied_into_logged_scrapy_settings():
    credential_names = {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
    assert credential_names.isdisjoint(vars(project_settings))

    settings = Settings()
    settings.setmodule(project_settings, priority="project")
    logged_overrides = dict(overridden_settings(settings))

    assert credential_names.isdisjoint(logged_overrides)
