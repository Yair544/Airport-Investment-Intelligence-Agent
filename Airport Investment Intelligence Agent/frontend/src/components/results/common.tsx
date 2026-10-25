import { formatDate, formatNumber, isNumber } from "../../lib/format";
import {
  conditionStatusLabel,
  constraintTypeLabel,
  evidenceScenarioLabel,
  evidenceStanceLabel,
  evidenceTopicLabel,
  signalStatusLabel,
  sourceLabel,
} from "../../lib/labels";
import type { AirportLabel, ConstraintRecord, EvidenceRecord, PeriodInfo, SignalStatus } from "../../types";
import { ExternalLink } from "../ExternalLink";

export function StatusBadge({ status, kind = "signal" }: { status: SignalStatus; kind?: "signal" | "condition" }) {
  const label = kind === "condition" ? conditionStatusLabel(status) : signalStatusLabel(status);
  return <span className={`badge badge--${status}`}>{label}</span>;
}

/** "LAX — Los Angeles International, CA" */
export function AirportName({ airport }: { airport: AirportLabel }) {
  return (
    <span className="airport">
      <span className="airport__code">{airport.code}</span>
      <span className="airport__name">
        {" "}
        — {airport.name}, {airport.state}
      </span>
    </span>
  );
}

/** Period chips; forecast periods are visually distinct from historical ones. Periods that differ
 * only by source (for example CY2025 from both T-100 and On-Time) share one chip. */
export function PeriodChips({ periods }: { periods: PeriodInfo[] }) {
  if (periods.length === 0) return null;
  const chips = new Map<string, { period: PeriodInfo; sources: string[] }>();
  for (const period of periods) {
    const key = `${period.label}|${period.nature}|${period.role ?? ""}`;
    const chip = chips.get(key) ?? { period, sources: [] };
    chip.sources.push(sourceLabel(period.source_id));
    chips.set(key, chip);
  }
  return (
    <ul className="chips" aria-label="Periods">
      {[...chips.entries()].map(([key, { period, sources }]) => (
        <li
          key={key}
          className={`chip chip--${period.nature}`}
          title={`${formatDate(period.start)} to ${formatDate(period.end)} · ${sources.join(", ")}`}
        >
          <span className="chip__label">{period.label}</span>
          <span className="chip__nature">{period.nature === "forecast" ? "Forecast" : "Historical"}</span>
          {period.role && <span className="chip__role">{period.role}</span>}
        </li>
      ))}
    </ul>
  );
}

export function Limitations({ items, title }: { items: string[]; title: string }) {
  if (items.length === 0) return null;
  return (
    <div className="limitations" role="note">
      <p className="limitations__title">{title}</p>
      <ul>
        {items.map((text, index) => (
          <li key={index} dir="auto">
            {text}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Notes({ notes }: { notes: string[] }) {
  if (notes.length === 0) return null;
  return (
    <ul className="notes">
      {notes.map((note, index) => (
        <li key={index} dir="auto">
          {note}
        </li>
      ))}
    </ul>
  );
}

export function ConstraintList({ constraints }: { constraints: ConstraintRecord[] }) {
  return (
    <ul className="records">
      {constraints.map((constraint, index) => (
        <li key={`${constraint.airport_id}-${constraint.constraint_type}-${index}`} className="record">
          <span className="badge badge--warn">{constraintTypeLabel(constraint.constraint_type)}</span>{" "}
          <span dir="auto">{constraint.description}</span>
          <span className="record__source">
            Source: <ExternalLink href={constraint.source_url}>{constraint.source_title}</ExternalLink> · checked{" "}
            {formatDate(constraint.checked_on)}
          </span>
        </li>
      ))}
    </ul>
  );
}

const STANCE_TONE: Record<EvidenceRecord["stance"], SignalStatus> = {
  supports: "pass",
  contradicts: "fail",
  insufficient: "unknown",
};

export function EvidenceList({ evidence }: { evidence: EvidenceRecord[] }) {
  return (
    <ul className="records">
      {evidence.map((record) => {
        const scenario = evidenceScenarioLabel(record.scenario);
        const facts = [
          isNumber(record.value) ? `Value: ${formatNumber(record.value, Number.isInteger(record.value) ? 0 : 1)}${record.unit ? ` ${record.unit}` : ""}` : null,
          record.activity_direction ? `Measures: ${record.activity_direction}` : null,
          record.period ? `Period: ${record.period}` : null,
          record.project_status ? `Project status: ${record.project_status}` : null,
          record.expected_operation ? `Expected operation: ${record.expected_operation}` : null,
        ].filter((fact): fact is string => fact !== null);
        return (
          <li key={record.evidence_id} className="record">
            <div className="record__head">
              <span className={`badge badge--${STANCE_TONE[record.stance]}`}>{evidenceStanceLabel(record)}</span>
              <span className="record__topic">
                {evidenceTopicLabel(record.topic)}
                {record.infrastructure_component ? ` · ${record.infrastructure_component}` : ""}
                {scenario ? ` · ${scenario}` : ""}
              </span>
            </div>
            <p className="record__text" dir="auto">
              {record.statement}
            </p>
            {facts.length > 0 && <p className="record__meta">{facts.join(" · ")}</p>}
            {record.assumptions && <p className="record__meta">Assumptions: {record.assumptions}</p>}
            {record.limitations && <p className="record__meta">Limitations: {record.limitations}</p>}
            <span className="record__source">
              Source: <ExternalLink href={record.source_url}>{record.source_title}</ExternalLink> ({record.source_locator})
              {record.source_date ? `, ${record.source_date}` : ""} · checked {formatDate(record.checked_on)}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
