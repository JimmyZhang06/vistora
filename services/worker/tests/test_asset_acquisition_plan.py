from framefactory.worker.asset_acquisition_plan import plan_acquisition_slots


def test_planner_covers_distinct_championship_and_training_beats() -> None:
    slots = plan_acquisition_slots(
        "张继科最快大满贯",
        (
            "2011年鹿特丹世乒赛男单决赛夺冠画面，持续4秒。",
            "2011年巴黎世界杯颁奖台与冠军奖杯，持续4秒。",
            "2012年伦敦奥运会男单决赛发球瞬间，持续4秒。",
            "张继科进行反手训练，镜头从侧面拍摄，持续4秒。",
        ),
        limit=12,
    )

    queries = [slot.query for slot in slots]
    assert any("2011年" in query and "世乒赛" in query for query in queries)
    assert any("世界杯" in query and "颁奖" in query for query in queries)
    assert any("2012年" in query and "奥运会" in query for query in queries)
    assert any("反手" in query and "训练" in query for query in queries)
    assert len(set(queries[:4])) == 4


def test_planner_places_topic_fallback_after_scene_coverage() -> None:
    slots = plan_acquisition_slots(
        "城市更新中的公共空间",
        ("旧住宅改造前的街道全景，持续5秒。", "居民在公共空间交流，持续5秒。"),
    )

    assert slots[0].key == "scene-01"
    assert slots[1].key == "scene-02"
    assert slots[-1].key == "topic-fallback"


def test_planner_keeps_each_authored_visual_beat_independent() -> None:
    slots = plan_acquisition_slots(
        "Solar Dynamics Observatory Sun eruption public domain footage",
        (
            "太阳表面爆发耀斑，强光向外扩散。",
            "地球磁场受扰动，极光在高纬度夜空闪烁。",
        ),
    )

    assert "太阳表面爆发耀斑" in slots[0].query
    assert "高纬度夜空" in slots[1].query
    assert "solar dynamics observatory" not in slots[1].query.casefold()


def test_planner_does_not_invent_provider_vocabulary_for_new_domains() -> None:
    slots = plan_acquisition_slots(
        "太阳风暴科普",
        (
            "NASA卫星图像显示CME轨迹，数据实时传输。",
            "地球磁层受扰，高能粒子进入极区大气。",
        ),
    )

    assert slots[0].query == "NASA卫星图像显示CME轨迹 数据实时传输"
    assert slots[1].query == "地球磁层受扰 高能粒子进入极区大气"
    assert "satellite" not in slots[0].query.casefold()


def test_planner_removes_edit_directions_without_a_sports_dictionary() -> None:
    slots = plan_acquisition_slots(
        "张继科最快大满贯：为什么445天如此罕见",
        (
            "张继科在鹿特丹世乒赛比赛中击球，字幕显示‘2011年5月15日’",
            "张继科在巴黎世界杯比赛中接发球，字幕显示‘2011年巴黎世界杯’",
            "张继科在伦敦奥运会比赛中对拉，字幕显示‘2012年8月2日’",
        ),
    )

    assert slots[0].query == "张继科在鹿特丹世乒赛比赛中击球 2011年"
    assert slots[1].query == "张继科在巴黎世界杯比赛中接发球 2011年"
    assert slots[2].query == "张继科在伦敦奥运会比赛中对拉 2012年"
    assert all("字幕" not in slot.query for slot in slots)
    assert all("最快大满贯" not in slot.query for slot in slots[:3])
