import {
  formatInteger,
  formatMiles,
  formatNauticalMiles,
  formatPercent,
  isNumber,
} from "../../lib/format";
import type { LongHaulResult } from "../../types";
import { AirportName } from "./common";
import { ResultFrame } from "./ResultFrame";

function ShareBar({ share }: { share: number | null }) {
  const width = isNumber(share) ? Math.min(100, Math.max(0, share)) : 0;
  return (
    <span className="bar" aria-hidden="true">
      <span className="bar__fill" style={{ width: `${width}%` }} />
    </span>
  );
}

export function LongHaulPanel({ result }: { result: LongHaulResult }) {
  const data = result.data;
  return (
    <ResultFrame result={result}>
      <div className="headline">
        <span className="headline__value">{formatPercent(data.long_haul_share)}</span>
        <span className="headline__text">
          of departures from <AirportName airport={data.airport} /> in {data.period_label} flew more than{" "}
          <strong>{formatMiles(data.threshold_miles)}</strong> ({formatNauticalMiles(data.threshold_nautical_miles)}).
        </span>
      </div>

      <dl className="stats">
        <div>
          <dt>Long-haul departures</dt>
          <dd>{formatInteger(data.long_haul_departures)}</dd>
        </div>
        <div>
          <dt>Departures with a valid distance</dt>
          <dd>{formatInteger(data.valid_departures)}</dd>
        </div>
        <div>
          <dt>All performed departures</dt>
          <dd>{formatInteger(data.total_departures)}</dd>
        </div>
        <div>
          <dt>Excluded: missing or zero distance</dt>
          <dd>
            {formatInteger(data.excluded_departures)}{" "}
            <span className="muted">({formatPercent(data.excluded_share)} of all departures)</span>
          </dd>
        </div>
      </dl>

      <h4 className="section-title">By aircraft configuration</h4>
      {data.by_aircraft_config.length === 0 ? (
        <p className="muted">No breakdown by aircraft configuration was returned.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Configuration</th>
                <th scope="col" className="num">
                  Long-haul departures
                </th>
                <th scope="col" className="num">
                  Valid-distance departures
                </th>
                <th scope="col" className="num">
                  Long-haul share
                </th>
              </tr>
            </thead>
            <tbody>
              {data.by_aircraft_config.map((row) => (
                <tr key={row.aircraft_config}>
                  <th scope="row">{row.label}</th>
                  <td className="num">{formatInteger(row.long_haul_departures)}</td>
                  <td className="num">{formatInteger(row.valid_departures)}</td>
                  <td className="num">{formatPercent(row.share)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4 className="section-title">Distribution by distance group</h4>
      {data.distance_groups.length === 0 ? (
        <p className="muted">No distance-group distribution was returned.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Distance group</th>
                <th scope="col" className="num">
                  Departures
                </th>
                <th scope="col" className="num">
                  Share
                </th>
                <th scope="col" className="bar-col">
                  <span className="visually-hidden">Share bar</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {data.distance_groups.map((row) => (
                <tr key={row.distance_group}>
                  <th scope="row">{row.label}</th>
                  <td className="num">{formatInteger(row.departures)}</td>
                  <td className="num">{formatPercent(row.share)}</td>
                  <td className="bar-col">
                    <ShareBar share={row.share} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h4 className="section-title">Top long-haul destinations</h4>
      {data.top_long_haul_destinations.length === 0 ? (
        <p className="muted">No departures above the threshold.</p>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Destination</th>
                <th scope="col" className="num">
                  Distance
                </th>
                <th scope="col" className="num">
                  Departures
                </th>
              </tr>
            </thead>
            <tbody>
              {data.top_long_haul_destinations.map((row) => (
                <tr key={row.dest_code}>
                  <th scope="row">
                    <span className="airport__code">{row.dest_code}</span>
                    {row.dest_name && <span className="airport__name"> — {row.dest_name}</span>}
                  </th>
                  <td className="num">{formatMiles(row.distance_miles)}</td>
                  <td className="num">{formatInteger(row.departures)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </ResultFrame>
  );
}
