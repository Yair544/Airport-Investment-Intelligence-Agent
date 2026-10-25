"""Metric calculations with fixed expected values (spec sections 4.2-4.6)."""

from __future__ import annotations

import pytest

from app.analysis import metrics
from tests.analysis_fakes import (
    ARRIVAL_COLUMNS,
    DEPARTURE_COLUMNS,
    T100_COLUMNS,
    TAF_COLUMNS,
    TAXI_COLUMNS,
    arrival_rows,
    departure_rows,
    frame,
    load_anc_t100_2025,
    segment,
    taf_rows,
    taxi_rows,
)

LF_COLUMNS = ["passengers", "seats", "departures_performed"]


# -- ratios, growth and CAGR ----------------------------------------------------------------------


def test_load_factor_is_a_ratio_of_sums_not_a_mean_of_percentages() -> None:
    rows = frame(
        [
            {"passengers": 90, "seats": 100, "departures_performed": 1},
            {"passengers": 10, "seats": 1_000, "departures_performed": 2},
        ],
        LF_COLUMNS,
    )
    result = metrics.load_factor(rows)
    assert result.value == pytest.approx(9.090909, abs=1e-6)  # 100 / 1,100, not (90% + 1%) / 2
    assert (result.passengers, result.seats, result.departures, result.rows) == (100, 1_100, 3, 2)


def test_load_factor_without_rows_or_seats_is_missing() -> None:
    empty = metrics.load_factor(frame([], T100_COLUMNS))
    assert (empty.value, empty.passengers, empty.seats, empty.rows) == (None, None, None, 0)
    zero_seats = metrics.load_factor(
        frame([{"passengers": 0, "seats": 0, "departures_performed": 1}], LF_COLUMNS)
    )
    assert zero_seats.value is None
    assert zero_seats.seats == 0


def test_cagr_percent() -> None:
    assert metrics.cagr_percent(1_000_000, 1_276_281.5625, 5) == pytest.approx(5.0, abs=1e-12)
    assert metrics.cagr_percent(1_000_000, 1_000_000, 5) == 0.0
    assert metrics.cagr_percent(0, 10, 5) is None
    assert metrics.cagr_percent(None, 10, 5) is None
    assert metrics.cagr_percent(10, None, 5) is None


def test_growth_and_percent_handle_zero_denominators() -> None:
    assert metrics.growth_percent(1_050, 1_000) == pytest.approx(5.0)
    assert metrics.growth_percent(950, 1_000) == pytest.approx(-5.0)
    assert metrics.growth_percent(5, 0) is None
    assert metrics.growth_percent(None, 1_000) is None
    assert metrics.percent(5, 0) is None
    assert metrics.percent(None, 10) is None
    assert metrics.percent(1, 8) == 12.5


def test_taf_growth_reports_delta_cagr_and_operations() -> None:
    # 3,200,000 x 1.05^5 = 4,084,101 exactly, so the five-year CAGR is 5%.
    rows = frame(
        taf_rows("BOS", {2024: (3_200_000, 100_000), 2027: (3_500_000, None), 2029: (4_084_101, 112_000)}),
        TAF_COLUMNS,
    )
    growth = metrics.taf_growth(rows, 2024, 2029)
    assert growth.base_enplanements == 3_200_000
    assert growth.horizon_enplanements == 4_084_101
    assert growth.delta_enplanements == 884_101
    assert growth.cagr == pytest.approx(5.0)
    assert (growth.base_operations, growth.horizon_operations, growth.delta_operations) == (
        100_000,
        112_000,
        12_000,
    )
    missing_ops = metrics.taf_growth(rows, 2024, 2027)
    assert missing_ops.delta_operations is None  # a missing value is never treated as zero
    assert missing_ops.delta_enplanements == 300_000


def test_national_taf_cagr_sums_airports_that_report_both_years() -> None:
    rows = frame(
        taf_rows("AAA", {2024: (1_000_000, 0), 2029: (1_100_000, 0)})
        + taf_rows("BBB", {2024: (0, 0), 2029: (50_000, 0)})
        + taf_rows("CCC", {2024: (500_000, 0)})  # no horizon year: left out
        + taf_rows("DDD", {2024: (None, 0), 2029: (10, 0)}),  # no base value: left out
        TAF_COLUMNS,
    )
    reference = metrics.national_taf_cagr(rows, 2024, 2029)
    assert (reference.metric, reference.period_key, reference.sample_size) == (
        "taf_cagr",
        "FY2024-FY2029",
        2,
    )
    assert reference.details["base_enplanements"] == 1_000_000
    assert reference.details["horizon_enplanements"] == 1_150_000
    assert reference.value == pytest.approx(100 * (1.15**0.2 - 1))  # 2.8346%


# -- taxi-time percentiles -------------------------------------------------------------------------


def test_grouped_quantile_on_a_symmetric_histogram() -> None:
    histogram = {10: 2, 11: 6, 12: 2}
    assert metrics.grouped_quantile(histogram, 0.5) == pytest.approx(11.0)
    assert metrics.grouped_quantile(histogram, 0.1) == pytest.approx(10.0)


def test_grouped_quantile_on_an_asymmetric_histogram_computed_by_hand() -> None:
    # n = 10. P10: rank 1 falls in the 8-minute bin (0 before, 1 inside): 7.5 + 1/1 = 8.5.
    # P50: rank 5 falls in the 10-minute bin (4 before, 4 inside): 9.5 + (5 - 4)/4 = 9.75.
    stats = metrics.taxi_stats({8: 1, 9: 3, 10: 4, 15: 2})
    assert (stats.sample, stats.p10, stats.median) == (10, 8.5, 9.75)
    assert stats.excess == pytest.approx(1.25)


def test_grouped_quantile_uses_an_exact_rank_at_bin_boundaries() -> None:
    # n = 30 and q = 0.1 give rank 3, reached exactly by the first bin: 3.5 + 3/3 = 4.5.
    # A floating-point rank (0.1 * 30 = 3.0000000000000004) would jump to the 7-minute bin.
    assert metrics.grouped_quantile({4: 3, 7: 27}, 0.1) == 4.5


def test_grouped_quantile_of_an_empty_histogram_is_missing() -> None:
    assert metrics.grouped_quantile({}, 0.5) is None
    assert metrics.grouped_quantile({5: 0}, 0.5) is None
    stats = metrics.taxi_stats({})
    assert (stats.sample, stats.median, stats.p10, stats.excess) == (0, None, None, None)
    with pytest.raises(ValueError):
        metrics.grouped_quantile({5: 1}, 1.5)


def test_histograms_are_merged_per_airport_across_months() -> None:
    rows = frame(
        taxi_rows(1, 2025, {10: 2, 11: 3}, month=1)
        + taxi_rows(1, 2025, {11: 3, 12: 2}, month=2)
        + taxi_rows(2, 2025, {20: 5, 21: 0}, month=1),
        TAXI_COLUMNS,
    )
    assert metrics.histograms_by_airport(rows) == {1: {10: 2, 11: 6, 12: 2}, 2: {20: 5}}
    assert metrics.histograms_by_airport(frame([], TAXI_COLUMNS)) == {}
    assert metrics.merge_histograms([{1: 1}, {1: 2, 3: 4}]) == {1: 3, 3: 4}


def test_national_taxi_out_excess_is_the_median_over_airports_with_enough_departures() -> None:
    histograms = {
        1: {10: 500, 11: 500},  # P10 9.7, median 10.5, excess 0.8
        2: {8: 100, 9: 100, 12: 600, 15: 200},  # P10 8.5, median 12.0, excess 3.5
        3: {10: 100, 12: 900},  # P10 10.5, median 11.5 + 400/900, excess 13/9
        4: {5: 100, 6: 100, 7: 800},  # P10 5.5, median 6.875, excess 11/8
        5: {10: 999},  # 999 departures: below the minimum, left out
    }
    rows = frame(
        [row for airport, hist in histograms.items() for row in taxi_rows(airport, 2025, hist)],
        TAXI_COLUMNS,
    )
    reference = metrics.national_taxi_out_excess(rows, 2025, 1_000)
    assert (reference.metric, reference.period_key, reference.sample_size) == (
        "ontime_taxi_out_excess_median",
        "CY2025",
        4,
    )
    assert reference.value == pytest.approx((11 / 8 + 13 / 9) / 2)  # mean of the middle pair
    assert reference.details == {"min_departures": 1_000, "airports": 4, "departures": 4_000}


def test_ordinary_median() -> None:
    assert metrics.median([3.0, 1.0, 2.0]) == 2.0
    assert metrics.median([4.0, 1.0, 2.0, 3.0]) == 2.5
    assert metrics.median([]) is None


def test_national_load_factor_uses_the_sums_of_all_origins() -> None:
    totals = frame(
        [
            {
                "origin_airport_id": 1,
                "origin": "AAA",
                "departures_performed": 10,
                "seats": 1_000,
                "passengers": 800,
                "months_present": 12,
            },
            {
                "origin_airport_id": 2,
                "origin": "BBB",
                "departures_performed": 1,
                "seats": 100,
                "passengers": 90,
                "months_present": 12,
            },
        ],
        ["origin_airport_id", "origin", "departures_performed", "seats", "passengers", "months_present"],
    )
    reference = metrics.national_load_factor(totals, 2025)
    assert (reference.metric, reference.period_key, reference.sample_size) == (
        "t100_load_factor",
        "CY2025",
        2,
    )
    assert reference.value == pytest.approx(100 * 890 / 1_100)
    assert (reference.details["passengers"], reference.details["seats"]) == (890, 1_100)


# -- long-haul share ---------------------------------------------------------------------------------


def test_long_haul_threshold_is_strict_and_invalid_distances_are_excluded() -> None:
    rows = frame(
        [
            segment(departures=10, distance=3_452, aircraft_config=1, dest="AAA"),  # equal: not long
            segment(departures=5, distance=3_453, aircraft_config=1, dest="BBB"),
            segment(departures=20, distance=3_500, aircraft_config=2, dest="CCC", dest_city_name="Ccc, CC"),
            segment(departures=7, distance=None, aircraft_config=2, dest="DDD"),  # missing distance
            segment(departures=3, distance=0, aircraft_config=1, dest="ORG"),  # returns to origin
            segment(departures=8, distance=400, aircraft_config=3, dest="EEE"),
            segment(departures=0, distance=5_000, aircraft_config=4, dest="FFF"),  # not performed
        ],
        T100_COLUMNS,
    )
    result = metrics.long_haul_breakdown(rows, 3_452)
    assert (result.total_departures, result.valid_departures, result.excluded_departures) == (53, 43, 10)
    assert result.long_haul_departures == 25
    assert result.long_haul_share == pytest.approx(100 * 25 / 43)
    assert result.excluded_share == pytest.approx(100 * 10 / 53)

    configs = {entry.aircraft_config: entry for entry in result.by_aircraft_config}
    assert list(configs) == [1, 2, 3]  # configuration 4 has no departures performed
    assert (configs[1].label, configs[1].long_haul_departures, configs[1].valid_departures) == (
        "Passenger",
        5,
        15,
    )
    assert configs[1].share == pytest.approx(100 / 3)
    assert (configs[2].label, configs[2].share) == ("Freight", 100.0)
    assert (configs[3].label, configs[3].share) == ("Passenger/freight combination", 0.0)
    assert sum(entry.valid_departures for entry in configs.values()) == result.valid_departures
    assert sum(entry.long_haul_departures for entry in configs.values()) == result.long_haul_departures

    groups = [(g.distance_group, g.label, g.departures) for g in result.distance_groups]
    assert groups == [(1, "< 500 mi", 8), (7, "3,000–3,499 mi", 15), (8, "3,500–3,999 mi", 20)]
    assert sum(g.share or 0 for g in result.distance_groups) == pytest.approx(100.0)

    destinations = [
        (d.dest_code, d.dest_name, d.distance_miles, d.departures) for d in result.top_destinations
    ]
    assert destinations == [("CCC", "Ccc, CC", 3_500, 20), ("BBB", None, 3_453, 5)]


def test_long_haul_uses_the_source_distance_group_and_handles_unknown_configuration() -> None:
    rows = frame(
        [
            segment(departures=4, distance=499, distance_group=2, aircraft_config=None),
            segment(departures=6, distance=1_200, aircraft_config=9),
        ],
        T100_COLUMNS,
    )
    result = metrics.long_haul_breakdown(rows, 3_452)
    assert [(g.distance_group, g.departures) for g in result.distance_groups] == [(2, 4), (3, 6)]
    assert [(c.aircraft_config, c.label) for c in result.by_aircraft_config] == [
        (0, "Other/unknown"),
        (9, "Other/unknown"),
    ]
    assert result.long_haul_share == 0.0


def test_long_haul_without_rows_or_valid_distances() -> None:
    empty = metrics.long_haul_breakdown(frame([], T100_COLUMNS), 3_452)
    assert (empty.total_departures, empty.long_haul_share, empty.excluded_share) == (0.0, None, None)
    no_distance = metrics.long_haul_breakdown(
        frame([segment(departures=5, distance=None)], T100_COLUMNS), 3_452
    )
    assert no_distance.long_haul_share is None
    assert no_distance.excluded_share == 100.0


@pytest.mark.parametrize(
    ("group", "label"),
    [(1, "< 500 mi"), (2, "500–999 mi"), (8, "3,500–3,999 mi"), (11, "5,000–5,499 mi")],
)
def test_distance_group_labels(group: int, label: str) -> None:
    assert metrics.distance_group_label(group) == label


def test_default_threshold_is_about_3000_nautical_miles() -> None:
    assert metrics.statute_to_nautical_miles(3_452) == pytest.approx(2_999.706, abs=1e-3)


def test_anc_long_haul_golden_values_from_the_bts_sample() -> None:
    """ANC departures in CY2025 from the BTS T-100 Segment sample (all service classes)."""
    result = metrics.long_haul_breakdown(load_anc_t100_2025(), 3_452)
    assert result.total_departures == 87_988
    assert result.excluded_departures == 242  # all ANC -> ANC with zero distance
    assert round(result.excluded_share or 0, 3) == 0.275
    assert result.valid_departures == 87_746
    assert result.long_haul_departures == 15_089
    assert result.long_haul_share == pytest.approx(100 * 15_089 / 87_746)
    assert round(result.long_haul_share or 0, 3) == 17.196

    configs = {entry.aircraft_config: entry for entry in result.by_aircraft_config}
    assert list(configs) == [1, 2, 3]
    passenger, freight, combination = configs[1], configs[2], configs[3]
    assert (passenger.long_haul_departures, passenger.valid_departures) == (492, 38_185)
    assert round(passenger.share or 0, 3) == 1.288
    assert (freight.long_haul_departures, freight.valid_departures) == (14_466, 48_769)
    assert round(freight.share or 0, 3) == 29.662
    assert (combination.long_haul_departures, combination.valid_departures) == (131, 792)
    assert sum(entry.valid_departures for entry in configs.values()) == 87_746
    assert sum(entry.long_haul_departures for entry in configs.values()) == 15_089

    assert sum(group.departures for group in result.distance_groups) == 87_746
    assert len(result.top_destinations) == 10
    top = result.top_destinations[0]
    assert (top.dest_code, top.departures, top.distance_miles) == ("HKG", 2_543, 5_081)
    assert [d.dest_code for d in result.top_destinations[:3]] == ["HKG", "MIA", "ICN"]


def test_anc_class_f_load_factor_from_the_bts_sample() -> None:
    rows = load_anc_t100_2025()
    result = metrics.load_factor(rows[rows["service_class"] == "F"])
    assert (result.passengers, result.seats, result.departures) == (2_636_007, 3_569_795, 36_040)
    assert result.value == pytest.approx(100 * 2_636_007 / 3_569_795)


# -- On-Time departures and delay causes -------------------------------------------------------


def test_departure_summary_counts_diversions_as_performed() -> None:
    rows = frame(
        departure_rows(
            1, 2025, scheduled=3_000, cancelled=30, diverted=5, del15_valid=2_970, del15_count=594
        ),
        DEPARTURE_COLUMNS,
    )
    summary = metrics.departure_summary(rows)
    assert (summary.months_covered, summary.days_covered) == (12, 365)
    assert (summary.scheduled, summary.cancelled, summary.performed) == (36_000, 360, 35_640)
    assert summary.performed_per_day == pytest.approx(35_640 / 365)
    assert (summary.delay_base, summary.delayed) == (35_640, 7_128)
    assert summary.delay_rate == pytest.approx(20.0)
    assert summary.cancellation_rate == pytest.approx(1.0)


def test_zero_denominator_nulls_only_the_metric_that_depends_on_it() -> None:
    departures = metrics.departure_summary(
        frame(
            departure_rows(1, 2025, scheduled=0, cancelled=0, del15_valid=0, del15_count=0, months=[1]),
            DEPARTURE_COLUMNS,
        )
    )
    assert departures.cancellation_rate is None
    assert departures.delay_rate is None
    assert (departures.performed, departures.performed_per_day, departures.days_covered) == (0, 0.0, 31)

    delays = metrics.delay_cause_summary(
        frame(
            arrival_rows(
                1,
                2025,
                arrivals=100,
                carrier=0,
                weather=0,
                nas=0,
                security=0,
                late_aircraft=0,
                months=[1],
            ),
            ARRIVAL_COLUMNS,
        )
    )
    assert (delays.arrivals, delays.attributed_minutes) == (100, 0.0)
    assert all(cause.minutes == 0 and cause.share is None for cause in delays.causes)

    missing = metrics.departure_summary(frame([], DEPARTURE_COLUMNS))
    assert (missing.months_covered, missing.scheduled, missing.performed_per_day) == (0, None, None)


def test_delay_cause_shares_and_monthly_breakdown() -> None:
    rows = frame(
        arrival_rows(
            1,
            2025,
            arrivals=3_000,
            carrier=1_000,
            weather=200,
            nas=600,
            security=0,
            late_aircraft=200,
            months=[1, 2],
        ),
        ARRIVAL_COLUMNS,
    )
    summary = metrics.delay_cause_summary(rows)
    assert (summary.arrivals, summary.attributed_minutes) == (6_000, 4_000)
    assert {cause.cause: cause.share for cause in summary.causes} == {
        "carrier": 50.0,
        "weather": 10.0,
        "nas": 30.0,
        "security": 0.0,
        "late_aircraft": 10.0,
    }
    monthly = metrics.monthly_delay_causes(rows, 2025)
    assert [entry.month for entry in monthly] == list(range(1, 13))
    assert (monthly[0].arrivals, monthly[0].attributed_delay_minutes) == (3_000, 2_000)
    march = monthly[2]
    assert (march.arrivals, march.attributed_delay_minutes) == (None, None)
    assert all(cause.minutes is None and cause.share is None for cause in march.causes)
