import type {
  ConstraintRecord,
  DelayCause,
  EvidenceRecord,
  RankingGroup,
  SignalStatus,
  SourceId,
} from "../types";
import { humanize } from "./format";

export const DELAY_CAUSES: readonly DelayCause[] = ["carrier", "weather", "nas", "security", "late_aircraft"];

export const DELAY_CAUSE_LABELS: Record<DelayCause, string> = {
  carrier: "Carrier",
  weather: "Extreme weather",
  nas: "NAS",
  security: "Security",
  late_aircraft: "Late aircraft",
};

/** Fallback labels; rows from the server carry their own `group_label`, which takes precedence. */
export const RANKING_GROUP_LABELS: Record<RankingGroup, string> = {
  strong_candidate: "Strong candidate",
  watch: "Watch",
  external_constraint: "Known constraint outside the terminal",
  information_needed: "Information needed",
  low_pressure: "Low pressure",
};

export const SIGNAL_LABELS = {
  S1: "Demand growth",
  S2: "Load factor",
  S3: "Taxi-out excess",
} as const;

export function signalStatusLabel(status: SignalStatus): string {
  return status === "pass" ? "Pass" : status === "fail" ? "Fail" : "Unknown";
}

export function conditionStatusLabel(status: SignalStatus): string {
  return status === "pass" ? "Met" : status === "fail" ? "Not met" : "Unknown";
}

const CONSTRAINT_TYPE_LABELS: Record<ConstraintRecord["constraint_type"], string> = {
  runway_airspace: "Runway / airspace",
  curfew: "Curfew",
  legal_cap: "Legal cap",
  slot_control: "Slot control",
  other: "Other constraint",
};

export function constraintTypeLabel(type: ConstraintRecord["constraint_type"]): string {
  return CONSTRAINT_TYPE_LABELS[type] ?? humanize(type);
}

const SOURCE_LABELS: Record<SourceId, string> = {
  faa_taf: "FAA Terminal Area Forecast",
  bts_t100_segment: "BTS T-100 Segment",
  bts_ontime: "BTS On-Time Performance",
  bts_t100_summary: "BTS T-100 summary API",
  constraints_table: "Constraints table",
  evidence_records: "Evidence records",
};

export function sourceLabel(id: string): string {
  return (SOURCE_LABELS as Record<string, string>)[id] ?? humanize(id);
}

const EVIDENCE_TOPIC_LABELS: Record<EvidenceRecord["topic"], string> = {
  demand: "Demand",
  capacity: "Capacity",
  constraint: "Constraint",
  expansion_plan: "Expansion plan",
};

const EVIDENCE_SCENARIO_LABELS: Record<EvidenceRecord["scenario"], string | null> = {
  existing: "Existing conditions",
  future_without_project: "Future without the project",
  future_with_project: "Future with the project",
  not_applicable: null,
};

const STANCE_SUBJECT_LABELS: Record<EvidenceRecord["stance_subject"], string> = {
  demand: "demand",
  constraint: "the constraint",
  expansion_fit: "the expansion fit",
};

export function evidenceTopicLabel(topic: EvidenceRecord["topic"]): string {
  return EVIDENCE_TOPIC_LABELS[topic] ?? humanize(topic);
}

export function evidenceScenarioLabel(scenario: EvidenceRecord["scenario"]): string | null {
  return EVIDENCE_SCENARIO_LABELS[scenario] ?? null;
}

/** "Supports demand", "Contradicts the constraint", "Insufficient on the expansion fit". */
export function evidenceStanceLabel(record: Pick<EvidenceRecord, "stance" | "stance_subject">): string {
  const subject = STANCE_SUBJECT_LABELS[record.stance_subject] ?? humanize(record.stance_subject);
  if (record.stance === "supports") return `Supports ${subject}`;
  if (record.stance === "contradicts") return `Contradicts ${subject}`;
  return `Insufficient on ${subject}`;
}
