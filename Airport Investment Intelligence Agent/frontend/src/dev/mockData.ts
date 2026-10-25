// Development and test fixtures: one sample ChatResponse per analysis type. The numbers are
// invented for interface work and are not real results. Never imported by the production path.

import type {
  AirportLabel,
  ChatResponse,
  ComparisonAirport,
  ComparisonResult,
  DelayCauseShare,
  LongHaulResult,
  PeriodInfo,
  RankingResult,
  RankingRow,
  SignalResult,
  SourceRef,
  UnmetDemandResult,
} from "../types";

const RETRIEVED_AT = "2026-10-20T08:30:00Z";
const DATA_VERSION = "dv_demo_2026_10";
const RULES_VERSION = "2026.10-1";
export const DEMO_LIMITATION = "Demo data for interface development; these are not real results.";

const SOURCES = {
  taf: {
    source_id: "faa_taf",
    name: "FAA Terminal Area Forecast",
    url: "https://taf.faa.gov/",
    period: "FY2024–FY2029",
    retrieved_at: RETRIEVED_AT,
    dataset_version: "TAF 2025 edition",
    data_version_id: DATA_VERSION,
  },
  t100: {
    source_id: "bts_t100_segment",
    name: "BTS T-100 Segment (All Carriers)",
    url: "https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FMG",
    period: "CY2024–CY2025",
    retrieved_at: RETRIEVED_AT,
    dataset_version: null,
    data_version_id: DATA_VERSION,
  },
  ontime: {
    source_id: "bts_ontime",
    name: "BTS Reporting Carrier On-Time Performance",
    url: "https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ",
    period: "CY2025",
    retrieved_at: RETRIEVED_AT,
    dataset_version: null,
    data_version_id: DATA_VERSION,
  },
  constraints: {
    source_id: "constraints_table",
    name: "Documented constraints table",
    url: "app/data/reference/constraints.csv",
    period: null,
    retrieved_at: null,
    dataset_version: "reference demo",
    data_version_id: null,
  },
} satisfies Record<string, SourceRef>;

const airport = (code: string, name: string, city: string, state: string): AirportLabel => ({
  airport_id: `apt_${code.toLowerCase()}`,
  code,
  name,
  city,
  state,
});

const SIGNAL_META = {
  S1: { name: "Demand growth", unit: "% per year" },
  S2: { name: "Load factor", unit: "%" },
  S3: { name: "Taxi-out excess", unit: "min" },
} as const;

const NATIONAL = { S1: 2.08, S2: 83.4, S3: 4.1 } as const;

function signal(
  id: "S1" | "S2" | "S3",
  value: number | null,
  status: SignalResult["status"],
  extra: Partial<SignalResult> = {},
): SignalResult {
  return { signal: id, ...SIGNAL_META[id], value, reference: NATIONAL[id], status, sample_size: null, note: null, ...extra };
}

const CY2025: PeriodInfo = {
  label: "CY2025",
  kind: "calendar_year",
  nature: "historical",
  source_id: "bts_ontime",
  start: "2025-01-01",
  end: "2025-12-31",
  role: "analysis year",
};

function rankingRow(row: Partial<RankingRow> & Pick<RankingRow, "rank" | "airport" | "group" | "group_label">): RankingRow {
  return {
    rule_id: "R2",
    rule: "S1 passes and at least one of S2 and S3 passes.",
    possible_groups: [],
    base_year_enplanements: null,
    horizon_enplanements: null,
    delta_enplanements: null,
    cagr_5: null,
    base_year_commercial_operations: null,
    horizon_commercial_operations: null,
    delta_commercial_operations: null,
    signals: [],
    constraints: [],
    evidence: [],
    evidence_status: "missing",
    further_review: false,
    notes: [],
    ...row,
  };
}

export const rankingResult: RankingResult = {
  analysis_type: "ranking",
  title: "Terminal expansion candidates in New England",
  completeness: "complete",
  units: { delta_enplanements: "enplaned passengers", cagr_5: "% per year", load_factor: "%", taxi_out_excess: "minutes" },
  parameters: {
    region: "New England",
    states: ["CT", "MA", "ME", "NH", "RI", "VT"],
    analysis_year: 2025,
    forecast_horizon_years: 5,
    min_enplanements: 10000,
    s3_min_departures: 1000,
  },
  defaults_applied: ["analysis_year", "forecast_horizon_years", "min_enplanements", "s3_min_departures"],
  periods: [
    { label: "FY2024", kind: "fiscal_year", nature: "historical", source_id: "faa_taf", start: "2023-10-01", end: "2024-09-30", role: "TAF base year" },
    { label: "FY2029", kind: "fiscal_year", nature: "forecast", source_id: "faa_taf", start: "2028-10-01", end: "2029-09-30", role: "forecast horizon" },
    CY2025,
  ],
  rules_version: RULES_VERSION,
  reference_version: "ref_demo_01",
  data_version_id: DATA_VERSION,
  sources: [SOURCES.taf, SOURCES.t100, SOURCES.ontime, SOURCES.constraints],
  limitations: [
    DEMO_LIMITATION,
    "On-Time data covers domestic flights of reporting carriers only, so S3 can be unknown at small airports.",
  ],
  data: {
    region_label: "New England",
    analysis_year: 2025,
    taf_edition: "TAF 2025",
    base_year: 2024,
    horizon_year: 2029,
    national_references: {
      S1: { metric: "taf_cagr", period_key: "FY2024–FY2029", value: NATIONAL.S1, sample_size: 412, details: {} },
      S2: { metric: "t100_load_factor", period_key: "CY2025", value: NATIONAL.S2, sample_size: null, details: {} },
      S3: { metric: "ontime_taxi_out_excess_median", period_key: "CY2025", value: NATIONAL.S3, sample_size: 318, details: {} },
    },
    rows: [
      rankingRow({
        rank: 1,
        airport: airport("BDL", "Bradley International", "Windsor Locks", "CT"),
        group: "strong_candidate",
        group_label: "Strong candidate",
        base_year_enplanements: 3_120_400,
        horizon_enplanements: 3_520_900,
        delta_enplanements: 400_500,
        cagr_5: 2.44,
        base_year_commercial_operations: 61_200,
        horizon_commercial_operations: 66_900,
        delta_commercial_operations: 5_700,
        signals: [signal("S1", 2.44, "pass"), signal("S2", 86.3, "pass"), signal("S3", 3.2, "fail", { sample_size: 30_115 })],
        further_review: true,
      }),
      rankingRow({
        rank: 2,
        airport: airport("PVD", "Rhode Island T. F. Green International", "Warwick", "RI"),
        group: "strong_candidate",
        group_label: "Strong candidate",
        base_year_enplanements: 2_010_300,
        horizon_enplanements: 2_233_100,
        delta_enplanements: 222_800,
        cagr_5: 2.12,
        base_year_commercial_operations: 39_800,
        horizon_commercial_operations: 42_100,
        delta_commercial_operations: 2_300,
        signals: [signal("S1", 2.12, "pass"), signal("S2", 83.42, "pass"), signal("S3", 4.3, "pass", { sample_size: 18_902 })],
        evidence_status: "available",
        evidence: [
          {
            evidence_id: "ev_demo_pvd_1",
            airport_id: "apt_pvd",
            topic: "capacity",
            infrastructure_component: "terminal",
            scenario: "existing",
            statement: "Demo statement: the master plan reports peak-hour gate use close to capacity.",
            value: 92,
            unit: "% of gates in use at peak",
            activity_direction: "departures",
            period: "2023",
            assumptions: null,
            limitations: "Sample record for interface testing.",
            project_status: null,
            expected_operation: null,
            stance_subject: "demand",
            stance: "supports",
            source_title: "Demo master plan",
            source_url: "https://example.com/demo-master-plan.pdf",
            source_locator: "Table 4-2",
            source_date: "2023-06",
            checked_on: "2026-10-01",
          },
        ],
      }),
      rankingRow({
        rank: 3,
        airport: airport("MHT", "Manchester-Boston Regional", "Manchester", "NH"),
        group: "watch",
        group_label: "Watch",
        rule_id: "R3",
        rule: "At least one signal passes.",
        base_year_enplanements: 820_100,
        horizon_enplanements: 879_000,
        delta_enplanements: 58_900,
        cagr_5: 1.4,
        base_year_commercial_operations: 21_000,
        horizon_commercial_operations: 21_900,
        delta_commercial_operations: 900,
        signals: [
          signal("S1", 1.4, "fail"),
          signal("S2", 84.1, "pass"),
          signal("S3", null, "unknown", { sample_size: 812, note: "Fewer than 1,000 departures with a valid taxi-out time." }),
        ],
        further_review: true,
      }),
      rankingRow({
        rank: 4,
        airport: airport("BOS", "General Edward Lawrence Logan International", "Boston", "MA"),
        group: "external_constraint",
        group_label: "Known constraint outside the terminal",
        rule_id: "R1",
        rule: "The airport has a documented constraint outside the terminal.",
        base_year_enplanements: 20_150_000,
        horizon_enplanements: 22_400_000,
        delta_enplanements: 2_250_000,
        cagr_5: 2.14,
        base_year_commercial_operations: 371_000,
        horizon_commercial_operations: 392_500,
        delta_commercial_operations: 21_500,
        signals: [signal("S1", 2.14, "pass"), signal("S2", 85.0, "pass"), signal("S3", 5.6, "pass", { sample_size: 151_204 })],
        constraints: [
          {
            airport_id: "apt_bos",
            constraint_type: "runway_airspace",
            description: "Demo record: airfield capacity limit used to exercise the constraint display.",
            source_title: "Demo constraint source",
            source_url: "https://example.com/demo-constraint",
            checked_on: "2026-10-01",
          },
        ],
        evidence_status: "available",
      }),
      rankingRow({
        rank: 5,
        airport: airport("PWM", "Portland International Jetport", "Portland", "ME"),
        group: "information_needed",
        group_label: "Information needed",
        rule_id: "UNKNOWN",
        rule: "The unknown signals could lead to different groups.",
        possible_groups: ["strong_candidate", "watch"],
        base_year_enplanements: 1_150_000,
        horizon_enplanements: 1_290_000,
        delta_enplanements: 140_000,
        cagr_5: 2.33,
        signals: [signal("S1", 2.33, "pass"), signal("S2", null, "unknown"), signal("S3", null, "unknown")],
        notes: ["Load factor above 100% in two months was flagged for review and left out."],
      }),
      rankingRow({
        rank: 6,
        airport: airport("BTV", "Patrick Leahy Burlington International", "South Burlington", "VT"),
        group: "low_pressure",
        group_label: "Low pressure",
        rule_id: "R4",
        rule: "No signal passes.",
        base_year_enplanements: 640_000,
        horizon_enplanements: 660_000,
        delta_enplanements: 20_000,
        cagr_5: 0.62,
        signals: [signal("S1", 0.62, "fail"), signal("S2", 79.9, "fail"), signal("S3", 2.1, "fail", { sample_size: 6_310 })],
      }),
    ],
    excluded: [
      {
        airport: airport("HYA", "Cape Cod Gateway", "Hyannis", "MA"),
        reason: "Fewer than 10,000 enplanements in FY2024.",
      },
    ],
  },
};

const causes = (carrier: number, weather: number, nas: number, security: number, late: number): DelayCauseShare[] => [
  { cause: "carrier", minutes: carrier * 1000, share: carrier },
  { cause: "weather", minutes: weather * 1000, share: weather },
  { cause: "nas", minutes: nas * 1000, share: nas },
  { cause: "security", minutes: security * 1000, share: security },
  { cause: "late_aircraft", minutes: late * 1000, share: late },
];

const comparisonAirport = (values: Partial<ComparisonAirport> & Pick<ComparisonAirport, "airport">): ComparisonAirport => ({
  days_covered: 365,
  departures_scheduled: null,
  departures_performed: null,
  departures_performed_per_day: null,
  departure_delay_rate: null,
  departure_delay_base: null,
  cancellation_rate: null,
  median_taxi_out: null,
  taxi_out_p10: null,
  taxi_out_excess: null,
  taxi_out_sample: null,
  median_taxi_in: null,
  taxi_in_sample: null,
  attributed_delay_minutes: null,
  delay_causes: [],
  constraints: [],
  notes: [],
  ...values,
});

export const comparisonResult: ComparisonResult = {
  analysis_type: "comparison",
  title: "Congestion comparison: LAX and SNA",
  completeness: "complete",
  units: { rates: "%", taxi_times: "minutes" },
  parameters: { airports: ["LAX", "SNA"], year: 2025 },
  defaults_applied: ["year"],
  periods: [CY2025],
  rules_version: RULES_VERSION,
  reference_version: "ref_demo_01",
  data_version_id: DATA_VERSION,
  sources: [SOURCES.ontime, SOURCES.constraints],
  limitations: [DEMO_LIMITATION],
  data: {
    year: 2025,
    period_label: "CY2025",
    airports: [
      comparisonAirport({
        airport: airport("LAX", "Los Angeles International", "Los Angeles", "CA"),
        departures_scheduled: 219_400,
        departures_performed: 216_950,
        departures_performed_per_day: 594.4,
        departure_delay_rate: 21.7,
        departure_delay_base: 216_100,
        cancellation_rate: 1.12,
        median_taxi_out: 17,
        taxi_out_p10: 11,
        taxi_out_excess: 6,
        taxi_out_sample: 216_100,
        median_taxi_in: 9,
        taxi_in_sample: 215_800,
        attributed_delay_minutes: 4_812_000,
        delay_causes: causes(33.1, 3.2, 21.4, 0.2, 42.1),
      }),
      comparisonAirport({
        airport: airport("SNA", "John Wayne Airport-Orange County", "Santa Ana", "CA"),
        departures_scheduled: 51_700,
        departures_performed: 51_320,
        departures_performed_per_day: 140.6,
        departure_delay_rate: 17.9,
        departure_delay_base: 51_200,
        cancellation_rate: 0.74,
        median_taxi_out: 14,
        taxi_out_p10: 10,
        taxi_out_excess: 4,
        taxi_out_sample: 51_200,
        median_taxi_in: 6,
        taxi_in_sample: 51_050,
        attributed_delay_minutes: 902_000,
        delay_causes: causes(36.8, 1.9, 14.2, 0.1, 47),
        constraints: [
          {
            airport_id: "apt_sna",
            constraint_type: "curfew",
            description: "Demo record: overnight departure restriction used to exercise the context list.",
            source_title: "Demo noise ordinance",
            source_url: "https://example.com/demo-ordinance",
            checked_on: "2026-10-01",
          },
        ],
        notes: ["Demo note: values cover reporting carriers only."],
      }),
    ],
  },
};

export const longHaulResult: LongHaulResult = {
  analysis_type: "long_haul_share",
  title: "Long-haul departures from Anchorage (ANC)",
  completeness: "complete",
  units: { distance: "statute miles", share: "%" },
  parameters: { airport: "ANC", year: 2025, threshold_miles: 3452 },
  defaults_applied: ["year", "threshold_miles"],
  periods: [{ ...CY2025, source_id: "bts_t100_segment" }],
  rules_version: RULES_VERSION,
  reference_version: null,
  data_version_id: DATA_VERSION,
  sources: [SOURCES.t100],
  limitations: [DEMO_LIMITATION, "The 3,452-mile threshold is a system choice, not a universal definition."],
  data: {
    airport: airport("ANC", "Ted Stevens Anchorage International", "Anchorage", "AK"),
    year: 2025,
    period_label: "CY2025",
    threshold_miles: 3452,
    threshold_nautical_miles: 3000,
    total_departures: 98_400,
    valid_departures: 97_950,
    long_haul_departures: 11_830,
    long_haul_share: 12.08,
    excluded_departures: 450,
    excluded_share: 0.46,
    by_aircraft_config: [
      { aircraft_config: 1, label: "Passenger", long_haul_departures: 210, valid_departures: 61_400, share: 0.34 },
      { aircraft_config: 2, label: "Freight", long_haul_departures: 11_620, valid_departures: 35_900, share: 32.37 },
      { aircraft_config: 3, label: "Passenger/freight combination", long_haul_departures: 0, valid_departures: 650, share: 0 },
    ],
    distance_groups: [
      { distance_group: 1, label: "Less than 500 mi", departures: 58_100, share: 59.32 },
      { distance_group: 2, label: "500-999 mi", departures: 9_400, share: 9.6 },
      { distance_group: 3, label: "1,000-1,499 mi", departures: 6_100, share: 6.23 },
      { distance_group: 4, label: "1,500-1,999 mi", departures: 4_300, share: 4.39 },
      { distance_group: 5, label: "2,000-2,499 mi", departures: 5_200, share: 5.31 },
      { distance_group: 6, label: "2,500-2,999 mi", departures: 2_900, share: 2.96 },
      { distance_group: 7, label: "3,000-3,499 mi", departures: 4_120, share: 4.21 },
      { distance_group: 8, label: "3,500-3,999 mi", departures: 3_900, share: 3.98 },
      { distance_group: 9, label: "4,000-4,499 mi", departures: 3_830, share: 3.91 },
    ],
    top_long_haul_destinations: [
      { dest_code: "ICN", dest_name: "Incheon International", distance_miles: 4_243, departures: 3_120 },
      { dest_code: "PVG", dest_name: "Shanghai Pudong International", distance_miles: 4_684, departures: 2_310 },
      { dest_code: "TPE", dest_name: "Taiwan Taoyuan International", distance_miles: 4_966, departures: 1_980 },
    ],
  },
};

const MONTHS = Array.from({ length: 12 }, (_, index) => index + 1);

export const unmetDemandResult: UnmetDemandResult = {
  analysis_type: "unmet_demand",
  title: "Unmet demand signs at San Francisco (SFO)",
  completeness: "partial",
  units: { growth: "%", load_factor: "%", taxi_out_excess: "minutes" },
  parameters: { airport: "SFO", year: 2025 },
  defaults_applied: ["year"],
  periods: [
    { ...CY2025, source_id: "bts_t100_segment" },
    { label: "CY2024", kind: "calendar_year", nature: "historical", source_id: "bts_t100_segment", start: "2024-01-01", end: "2024-12-31", role: "comparison year" },
  ],
  rules_version: RULES_VERSION,
  reference_version: "ref_demo_01",
  data_version_id: DATA_VERSION,
  sources: [SOURCES.t100, SOURCES.ontime, SOURCES.constraints],
  limitations: [DEMO_LIMITATION, "December 2025 delay causes were not available, so the monthly table is incomplete."],
  data: {
    airport: airport("SFO", "San Francisco International", "San Francisco", "CA"),
    analysis_year: 2025,
    comparison_year: 2024,
    classification: "constrained_demand_signs",
    classification_label: "Signs of constrained demand",
    rule: "S2 passes, passenger growth exceeds seat growth, and S3 passes or a documented constraint exists.",
    conditions: [
      { condition_id: "S2", description: "Load factor at or above the national reference", status: "pass", detail: "86.9% vs 83.4%" },
      { condition_id: "PAX_VS_SEATS", description: "Passenger growth above seat growth", status: "pass", detail: "+4.1% vs +2.6%" },
      { condition_id: "S3_OR_CONSTRAINT", description: "Taxi-out excess passes or a documented constraint exists", status: "pass", detail: "S3 passes (6.4 vs 4.1 min)" },
    ],
    passengers: 12_310_000,
    passengers_previous: 11_825_000,
    passenger_growth: 4.1,
    seats: 14_160_000,
    seats_previous: 13_800_000,
    seat_growth: 2.6,
    s2: signal("S2", 86.9, "pass"),
    s3: signal("S3", 6.4, "pass", { sample_size: 141_330 }),
    constraints: [],
    evidence: [],
    evidence_status: "missing",
    monthly_delay_causes: MONTHS.slice(0, 11).map((month) => ({
      year: 2025,
      month,
      arrivals: 11_000 + month * 120,
      attributed_delay_minutes: 210_000 + month * 4_000,
      causes: causes(30 + (month % 3), 2 + (month % 4), 30 - (month % 5), 0.2, 37.8 - (month % 3) - (month % 4) + (month % 5)),
    })),
  },
};

function response(answer: string, results: ChatResponse["results"], conversationId = "conv_demo"): ChatResponse {
  const sources = results.flatMap((result) => result.sources);
  const unique = sources.filter((source, index) => sources.findIndex((s) => s.source_id === source.source_id) === index);
  return { conversation_id: conversationId, answer, results, sources: unique, limitations: [DEMO_LIMITATION] };
}

export const sampleResponses = {
  ranking: response(
    "**Two New England airports are strong candidates** under the deterministic rules: BDL and PVD.\n\n" +
      "- BDL has the largest forecast passenger increase among them, but no terminal evidence is on file, so it is a candidate for further review.\n" +
      "- BOS is grouped separately because of a documented constraint outside the terminal.\n\n" +
      "Forecast values are FAA TAF fiscal years; see the [FAA TAF site](https://taf.faa.gov/). " +
      "Unsafe links such as [this one](javascript:alert(1)) are shown as plain text, and images are not loaded: ![remote](https://example.com/x.png)",
    [rankingResult],
  ),
  comparison: response(
    "LAX shows more operational congestion than SNA in CY2025: a higher departure delay rate and a larger taxi-out excess (6 vs 4 minutes). There is no combined score.",
    [comparisonResult],
  ),
  longHaul: response(
    "About **12.1%** of departures from Anchorage flew more than 3,452 miles in CY2025, almost all of them freighters.",
    [longHaulResult],
  ),
  unmetDemand: response(
    "SFO shows **signs of constrained demand**: load factor is above the national reference and passengers grew faster than seats.",
    [unmetDemandResult],
  ),
  clarification: response(
    "Which airports or region should I analyze? For example, you can ask me to rank New England airports or compare LAX and SNA.",
    [],
  ),
  hebrew: response(
    "**שדה התעופה SFO** מציג סימנים לביקוש מוגבל.\n\n- מקדם התפוסה גבוה מהייחוס הלאומי.\n- צמיחת הנוסעים גבוהה מצמיחת המושבים.",
    [],
  ),
} satisfies Record<string, ChatResponse>;
