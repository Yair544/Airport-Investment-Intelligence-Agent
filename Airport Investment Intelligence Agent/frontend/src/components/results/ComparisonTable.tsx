import type { ReactNode } from "react";
import {
  formatInteger,
  formatMinutes,
  formatNumber,
  formatPercent,
  formatSample,
  isNumber,
  NO_DATA,
} from "../../lib/format";
import { DELAY_CAUSE_LABELS, DELAY_CAUSES } from "../../lib/labels";
import type { ComparisonAirport, ComparisonResult } from "../../types";
import { AirportName, ConstraintList, Notes } from "./common";
import { ResultFrame } from "./ResultFrame";

interface MetricRow {
  label: string;
  hint?: string;
  value: (airport: ComparisonAirport) => ReactNode;
}

function withSample(text: string, sample: number | null): ReactNode {
  const suffix = formatSample(sample);
  return (
    <>
      <span className="cell-main">{text}</span>
      {suffix && text !== NO_DATA && <span className="cell-sub">{suffix}</span>}
    </>
  );
}

function departures(airport: ComparisonAirport): string {
  if (!isNumber(airport.departures_performed)) return NO_DATA;
  const performed = formatInteger(airport.departures_performed);
  return isNumber(airport.departures_scheduled)
    ? `${performed} of ${formatInteger(airport.departures_scheduled)} scheduled`
    : performed;
}

const METRICS: MetricRow[] = [
  {
    label: "Departures per day",
    hint: "performed",
    value: (a) => formatNumber(a.departures_performed_per_day, 1),
  },
  { label: "Departures", hint: "performed in the period", value: (a) => departures(a) },
  {
    label: "Departure delay rate",
    hint: "15+ min late, % of non-cancelled departures",
    value: (a) => withSample(formatPercent(a.departure_delay_rate), a.departure_delay_base),
  },
  {
    label: "Cancellation rate",
    hint: "% of scheduled departures",
    value: (a) => formatPercent(a.cancellation_rate),
  },
  { label: "Median taxi-out", hint: "departures", value: (a) => formatMinutes(a.median_taxi_out) },
  { label: "Taxi-out P10", hint: "unimpeded proxy", value: (a) => formatMinutes(a.taxi_out_p10) },
  {
    label: "Taxi-out excess",
    hint: "median − P10",
    value: (a) => withSample(formatMinutes(a.taxi_out_excess), a.taxi_out_sample),
  },
  {
    label: "Median taxi-in",
    hint: "arrivals",
    value: (a) => withSample(formatMinutes(a.median_taxi_in), a.taxi_in_sample),
  },
  {
    label: "Attributed arrival delay",
    hint: "minutes with a reported cause",
    value: (a) => (isNumber(a.attributed_delay_minutes) ? `${formatInteger(a.attributed_delay_minutes)} min` : NO_DATA),
  },
  { label: "Days covered", value: (a) => formatInteger(a.days_covered) },
];

function causeShare(airport: ComparisonAirport, cause: (typeof DELAY_CAUSES)[number]): string {
  return formatPercent(airport.delay_causes.find((entry) => entry.cause === cause)?.share);
}

export function ComparisonTable({ result }: { result: ComparisonResult }) {
  const { airports, period_label: period } = result.data;
  const withConstraints = airports.filter((airport) => airport.constraints.length > 0);
  const withNotes = airports.filter((airport) => airport.notes.length > 0);

  return (
    <ResultFrame result={result}>
      <p className="result__meta">
        {period} · Departures and taxi times from BTS On-Time reporting carriers · No combined score
      </p>
      {airports.length === 0 ? (
        <p className="empty-note">No airport data was returned.</p>
      ) : (
        <div className="table-wrap">
          <table className="data data--comparison">
            <caption className="visually-hidden">Congestion metrics by airport for {period}</caption>
            <thead>
              <tr>
                <th scope="col" className="sticky-col">
                  Metric
                </th>
                {airports.map((airport) => (
                  <th key={airport.airport.airport_id} scope="col" className="num">
                    <AirportName airport={airport.airport} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {METRICS.map((metric) => (
                <tr key={metric.label}>
                  <th scope="row" className="sticky-col">
                    {metric.label}
                    {metric.hint && <span className="th-sub">{metric.hint}</span>}
                  </th>
                  {airports.map((airport) => (
                    <td key={airport.airport.airport_id} className="num">
                      {metric.value(airport)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
            <tbody>
              <tr className="group-row">
                <th scope="colgroup" colSpan={airports.length + 1}>
                  <span className="group-row__label">
                    Delay causes <span className="group-row__count">share of attributed arrival delay minutes</span>
                  </span>
                </th>
              </tr>
              {DELAY_CAUSES.map((cause) => (
                <tr key={cause}>
                  <th scope="row" className="sticky-col">
                    {DELAY_CAUSE_LABELS[cause]}
                  </th>
                  {airports.map((airport) => (
                    <td key={airport.airport.airport_id} className="num">
                      {causeShare(airport, cause)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="footnote">NAS (National Airspace System) delays also include non-extreme weather and air traffic control.</p>

      <h4 className="section-title">Documented constraints (context)</h4>
      {withConstraints.length === 0 ? (
        <p className="muted">No documented constraints on file for these airports.</p>
      ) : (
        withConstraints.map((airport) => (
          <div key={airport.airport.airport_id} className="airport-records">
            <p className="airport-records__title">
              <AirportName airport={airport.airport} />
            </p>
            <ConstraintList constraints={airport.constraints} />
          </div>
        ))
      )}

      {withNotes.length > 0 && (
        <>
          <h4 className="section-title">Notes</h4>
          {withNotes.map((airport) => (
            <div key={airport.airport.airport_id} className="airport-records">
              <p className="airport-records__title">
                <AirportName airport={airport.airport} />
              </p>
              <Notes notes={airport.notes} />
            </div>
          ))}
        </>
      )}
    </ResultFrame>
  );
}
