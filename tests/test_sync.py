from app.sync import decode_groups, latest_daily_weight, merge_bodyweight


def test_withings_exponent_and_timezone():
    records = decode_groups([{"grpid":22,"date":1792242000,"measures":[
        {"type":1,"value":88750,"unit":-3},{"type":6,"value":176,"unit":-1}]}])
    assert records[0]["weight_kg"] == 88.75
    assert records[0]["fat_pct"] == 17.6


def test_prefer_withings_keeps_routines_and_idempotence():
    origin = {"unit":"kg", "routines":[{"id":"A"}], "workouts":[{"id":"w"}],
              "bodyweight":[{"d":"2026-10-17","w":92.0,"manual":True}],"_rev":17}
    raw = [{"day":"2026-10-17", "weight_kg":89.23,"timestamp":1792251000,"group_id":"123"}]
    result, stats = merge_bodyweight(origin, raw, True)
    assert stats["replaced"] == 1
    assert result["bodyweight"][0]["w"] == 89.2
    assert result["bodyweight"][0]["manual"] is True
    assert result["workouts"] == origin["workouts"] and result["routines"] == origin["routines"]
    again, counts = merge_bodyweight(result, raw, True)
    assert counts["unchanged"] == 1
    assert again == result


def test_dont_overwrite_when_disabled():
    origin = {"bodyweight":[{"d":"2026-10-17","w":90.1}]}
    raw = [{"day":"2026-10-17", "weight_kg":89.23,"timestamp":1792251000}]
    result, counts = merge_bodyweight(origin, raw, False)
    assert result["bodyweight"][0]["w"] == 90.1
    assert counts["unchanged"] == 1


def test_new_day_and_lbs():
    origin = {"unit":"lb","bodyweight":[]}
    result, counts = merge_bodyweight(origin, [{"day":"2026-10-17","weight_kg":80.0,"timestamp":1792251000}])
    assert result["bodyweight"][0]["w"] == 176.4
    assert counts["added"] == 1
