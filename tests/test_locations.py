from latam_jobs.locations import country_list_label, location_label


def test_many_countries_are_counted_instead_of_listed() -> None:
    names = ["Argentina", "Brazil", "Chile", "Colombia"]
    assert country_list_label(names) == "4 countries"
    assert country_list_label(names, zone="LATAM") == "LATAM: 4 countries"
    assert country_list_label(["Brazil", "Chile", "Colombia"]) == "Brazil, Chile, Colombia"
    assert (
        location_label(
            "Argentina, Brazil, Chile, Colombia, Peru",
            ("argentina", "brazil", "chile", "colombia", "peru"),
        )
        == "5 countries"
    )
    assert (
        location_label(
            "LATAM: Argentina, Brazil, Chile, Colombia",
            ("argentina", "brazil", "chile", "colombia"),
        )
        == "LATAM: 4 countries"
    )
    assert location_label("Brazil", ("brazil",)) == "Brazil"
