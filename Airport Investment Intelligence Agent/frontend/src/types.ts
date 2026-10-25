// Response and request types mirrored from backend/app/api/schemas.py and
// backend/app/analysis/models.py. Percentages are percent values (84.2 means 84.2%),
// distances are statute miles, durations are minutes. Missing values are null.

export type SignalStatus = "pass" | "fail" | "unknown";
export type SourceId =
  | "faa_taf"
  | "bts_t100_segment"
  | "bts_ontime"
  | "bts_t100_summary"
  | "constraints_table"
  | "evidence_records";
export type DelayCause = "carrier" | "weather" | "nas" | "security" | "late_aircraft";

// ---------------------------------------------------------------------------------------------
// Errors and auth
// ---------------------------------------------------------------------------------------------

export interface ErrorBody {
  code: string;
  message: string;
  request_id?: string | null;
  conversation_id?: string | null;
}

export interface ErrorEnvelope {
  error: ErrorBody;
}

export interface User {
  user_id: string;
  username: string;
}

export interface AuthResponse {
  user: User;
  csrf_token: string;
}

// ---------------------------------------------------------------------------------------------
// Provenance and periods
// ---------------------------------------------------------------------------------------------

export interface SourceRef {
  source_id: SourceId;
  name: string;
  url: string;
  period: string | null;
  retrieved_at: string | null; // ISO 8601
  dataset_version: string | null;
  data_version_id: string | null;
}

export interface PeriodInfo {
  label: string;
  kind: "calendar_year" | "calendar_months" | "fiscal_year";
  nature: "historical" | "forecast";
  source_id: SourceId;
  start: string; // YYYY-MM-DD
  end: string;
  role: string | null;
}

export interface NationalReference {
  metric: "taf_cagr" | "t100_load_factor" | "ontime_taxi_out_excess_median";
  period_key: string;
  value: number | null;
  sample_size: number | null;
  details: Record<string, number | string | null>;
}

export interface ConstraintRecord {
  airport_id: string;
  constraint_type: "runway_airspace" | "curfew" | "legal_cap" | "slot_control" | "other";
  description: string;
  source_title: string;
  source_url: string;
  checked_on: string;
}

export interface EvidenceRecord {
  evidence_id: string;
  airport_id: string;
  topic: "demand" | "capacity" | "constraint" | "expansion_plan";
  infrastructure_component: string | null;
  scenario: "existing" | "future_without_project" | "future_with_project" | "not_applicable";
  statement: string;
  value: number | null;
  unit: string | null;
  activity_direction: string | null;
  period: string | null;
  assumptions: string | null;
  limitations: string | null;
  project_status: string | null;
  expected_operation: string | null;
  stance_subject: "demand" | "constraint" | "expansion_fit";
  stance: "supports" | "contradicts" | "insufficient";
  source_title: string;
  source_url: string;
  source_locator: string;
  source_date: string | null;
  checked_on: string;
}

export interface AirportLabel {
  airport_id: string;
  code: string;
  name: string;
  city: string;
  state: string;
}

export interface SignalResult {
  signal: "S1" | "S2" | "S3";
  name: string;
  value: number | null;
  reference: number | null;
  status: SignalStatus;
  unit: string;
  sample_size: number | null;
  note: string | null;
}

// ---------------------------------------------------------------------------------------------
// Ranking
// ---------------------------------------------------------------------------------------------

export type RankingGroup =
  | "strong_candidate"
  | "watch"
  | "external_constraint"
  | "information_needed"
  | "low_pressure";

export interface RankingRow {
  rank: number;
  airport: AirportLabel;
  group: RankingGroup;
  group_label: string;
  rule_id: "R1" | "R2" | "R3" | "R4" | "UNKNOWN";
  rule: string;
  possible_groups: RankingGroup[];
  base_year_enplanements: number | null;
  horizon_enplanements: number | null;
  delta_enplanements: number | null;
  cagr_5: number | null;
  base_year_commercial_operations: number | null;
  horizon_commercial_operations: number | null;
  delta_commercial_operations: number | null;
  signals: SignalResult[];
  constraints: ConstraintRecord[];
  evidence: EvidenceRecord[];
  evidence_status: "available" | "missing";
  further_review: boolean; // strong/watch airport without terminal evidence on file
  notes: string[];
}

export interface ExcludedAirport {
  airport: AirportLabel;
  reason: string;
}

export interface RankingData {
  region_label: string | null;
  analysis_year: number;
  taf_edition: string;
  base_year: number;
  horizon_year: number;
  national_references: Partial<Record<"S1" | "S2" | "S3", NationalReference>>;
  rows: RankingRow[];
  excluded: ExcludedAirport[];
}

// ---------------------------------------------------------------------------------------------
// Comparison
// ---------------------------------------------------------------------------------------------

export interface DelayCauseShare {
  cause: DelayCause;
  minutes: number | null;
  share: number | null;
}

export interface ComparisonAirport {
  airport: AirportLabel;
  days_covered: number | null;
  departures_scheduled: number | null;
  departures_performed: number | null;
  departures_performed_per_day: number | null;
  departure_delay_rate: number | null;
  departure_delay_base: number | null;
  cancellation_rate: number | null;
  median_taxi_out: number | null;
  taxi_out_p10: number | null;
  taxi_out_excess: number | null;
  taxi_out_sample: number | null;
  median_taxi_in: number | null;
  taxi_in_sample: number | null;
  attributed_delay_minutes: number | null;
  delay_causes: DelayCauseShare[];
  constraints: ConstraintRecord[];
  notes: string[];
}

export interface ComparisonData {
  year: number;
  period_label: string;
  airports: ComparisonAirport[];
}

// ---------------------------------------------------------------------------------------------
// Long-haul share
// ---------------------------------------------------------------------------------------------

export interface AircraftConfigShare {
  aircraft_config: number;
  label: string;
  long_haul_departures: number;
  valid_departures: number;
  share: number | null;
}

export interface DistanceGroupRow {
  distance_group: number;
  label: string;
  departures: number;
  share: number | null;
}

export interface DestinationRow {
  dest_code: string;
  dest_name: string | null;
  distance_miles: number;
  departures: number;
}

export interface LongHaulData {
  airport: AirportLabel;
  year: number;
  period_label: string;
  threshold_miles: number;
  threshold_nautical_miles: number;
  total_departures: number;
  valid_departures: number;
  long_haul_departures: number;
  long_haul_share: number | null;
  excluded_departures: number;
  excluded_share: number | null;
  by_aircraft_config: AircraftConfigShare[];
  distance_groups: DistanceGroupRow[];
  top_long_haul_destinations: DestinationRow[];
}

// ---------------------------------------------------------------------------------------------
// Unmet demand
// ---------------------------------------------------------------------------------------------

export interface ConditionResult {
  condition_id: string;
  description: string;
  status: SignalStatus;
  detail: string | null;
}

export interface MonthlyDelayCauses {
  year: number;
  month: number;
  arrivals: number | null;
  attributed_delay_minutes: number | null;
  causes: DelayCauseShare[];
}

export interface UnmetDemandData {
  airport: AirportLabel;
  analysis_year: number;
  comparison_year: number;
  classification: "constrained_demand_signs" | "weak_demand_signs" | "undetermined";
  classification_label: string;
  rule: string;
  conditions: ConditionResult[];
  passengers: number | null;
  passengers_previous: number | null;
  passenger_growth: number | null;
  seats: number | null;
  seats_previous: number | null;
  seat_growth: number | null;
  s2: SignalResult;
  s3: SignalResult;
  constraints: ConstraintRecord[];
  evidence: EvidenceRecord[];
  evidence_status: "available" | "missing";
  monthly_delay_causes: MonthlyDelayCauses[];
}

// ---------------------------------------------------------------------------------------------
// Analysis result union
// ---------------------------------------------------------------------------------------------

export interface ResultBase {
  title: string;
  completeness: "complete" | "partial";
  units: Record<string, string>;
  parameters: Record<string, string | number | boolean | string[] | null>;
  defaults_applied: string[]; // parameter names that used the system default
  periods: PeriodInfo[];
  rules_version: string;
  reference_version: string | null;
  data_version_id: string | null;
  sources: SourceRef[];
  limitations: string[];
}

export interface RankingResult extends ResultBase {
  analysis_type: "ranking";
  data: RankingData;
}

export interface ComparisonResult extends ResultBase {
  analysis_type: "comparison";
  data: ComparisonData;
}

export interface LongHaulResult extends ResultBase {
  analysis_type: "long_haul_share";
  data: LongHaulData;
}

export interface UnmetDemandResult extends ResultBase {
  analysis_type: "unmet_demand";
  data: UnmetDemandData;
}

export type AnalysisResult = RankingResult | ComparisonResult | LongHaulResult | UnmetDemandResult;

// ---------------------------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------------------------

export type ChatStatus = "processing" | "completed" | "failed" | "interrupted";

export interface ChatRequestBody {
  request_id: string; // UUID generated once per new message
  message: string;
  conversation_id: string | null;
}

export interface ChatResponse {
  conversation_id: string;
  answer: string;
  results: AnalysisResult[];
  sources: SourceRef[];
  limitations: string[];
}

export interface ChatAccepted {
  request_id: string;
  conversation_id: string;
  status: ChatStatus;
  status_url: string;
}

export interface ChatRequestStatus {
  request_id: string;
  conversation_id: string;
  status: ChatStatus;
  created_at: string;
  updated_at: string;
  response: ChatResponse | null;
  error: ErrorBody | null;
}

// ---------------------------------------------------------------------------------------------
// Conversations
// ---------------------------------------------------------------------------------------------

export interface ConversationSummary {
  conversation_id: string;
  title: string;
  updated_at: string;
}

export interface ConversationList {
  conversations: ConversationSummary[];
}

export interface MessageRequestInfo {
  request_id: string;
  status: ChatStatus;
  sent_conversation_id: string | null;
}

export interface MessageOut {
  seq: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  request: MessageRequestInfo | null;
  response: ChatResponse | null;
}

export interface ConversationDetail {
  conversation_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: MessageOut[];
}

// ---------------------------------------------------------------------------------------------
// Data refresh
// ---------------------------------------------------------------------------------------------

export type RefreshStatus = "queued" | "running" | "completed" | "failed" | "interrupted";

export interface SourceRefreshResult {
  source_id: string;
  status: "pending" | "unchanged" | "updated" | "failed" | "skipped";
  periods: string[];
  checked_at: string | null;
  retrieved_at: string | null;
  message: string | null;
}

export interface RefreshJob {
  job_id: string;
  status: RefreshStatus;
  stage: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  sources: SourceRefreshResult[];
  changed: boolean | null;
  active_version_id: string | null;
  error: ErrorBody | null;
  status_url: string;
}
