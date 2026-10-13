from jarvis.tools import _geocode_candidates, _haversine_m, _nearby_osm_filter


def test_geocode_candidates_prefers_home_city() -> None:
    candidates = _geocode_candidates("徐家汇", "上海")
    assert candidates[0] == "上海徐家汇"
    assert "徐家汇" in candidates
    assert "上海" in candidates


def test_nearby_osm_filter_maps_common_categories() -> None:
    assert _nearby_osm_filter("咖啡店") == '["amenity"="cafe"]'
    assert _nearby_osm_filter("火锅") == '["amenity"~"^(restaurant|fast_food)$"]'
    assert _nearby_osm_filter("24小时便利店") == '["shop"="convenience"]'
    assert _nearby_osm_filter("abcdef") is None


def test_haversine_distance_is_reasonable() -> None:
    distance = _haversine_m(31.2304, 121.4737, 31.2324, 121.4737)
    assert 205 <= distance <= 240
    assert _haversine_m(31.2304, 121.4737, 31.2304, 121.4737) == 0
