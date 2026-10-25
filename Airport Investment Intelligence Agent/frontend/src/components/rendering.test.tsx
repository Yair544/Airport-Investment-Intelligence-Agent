import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { comparisonResult, longHaulResult, rankingResult, sampleResponses, unmetDemandResult } from "../dev/mockData";
import { createApiClient } from "../api/client";
import type { AssistantItem, UserItem } from "../state/chatStore";
import { Login } from "./Login";
import { Markdown } from "./Markdown";
import { Message } from "./Message";
import { ResultsTable } from "./ResultsTable";

const noop = () => {};

describe("Markdown", () => {
  it("keeps http(s) links and drops raw HTML, images and unsafe URLs", () => {
    const html = renderToStaticMarkup(
      <Markdown
        text={[
          "**Bold** [FAA](https://taf.faa.gov/) [bad](javascript:alert(1)) [relative](/api/conversations)",
          "",
          "<script>alert(1)</script><b>raw</b><img src=x onerror=alert(1)>",
          "",
          "![tracker](https://example.com/pixel.png)",
        ].join("\n")}
      />,
    );
    expect(html).toContain('<a href="https://taf.faa.gov/" target="_blank" rel="noopener noreferrer nofollow">FAA</a>');
    expect(html).toContain("<strong>Bold</strong>");
    expect(html).toContain('<p dir="auto">');
    expect(html).not.toContain("javascript:");
    expect(html).not.toContain('href="/api/conversations"');
    expect(html).not.toContain("<script");
    expect(html).not.toContain("<b>");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("onerror");
  });
});

describe("result panels", () => {
  it("renders the ranking with groups in server order, references and review flags", () => {
    const html = renderToStaticMarkup(<ResultsTable result={rankingResult} />);
    const order = ["Strong candidate", "Watch", "Known constraint outside the terminal", "Information needed", "Low pressure"].map(
      (label) => html.indexOf(`${label} <span class="group-row__count">`),
    );
    expect(order.every((position) => position > 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(html).toContain("National references");
    expect(html).toContain("86.3% vs 83.4%");
    expect(html).toContain("No data vs 4.1 min");
    expect(html).toContain(">Unknown<");
    expect(html).toContain("Further review");
    expect(html).toContain("1 airport excluded");
    expect(html).toContain("Calculation details");
    expect(html).toMatch(/2025<span class="badge badge--neutral badge--inline">default<\/span>/);
  });

  it("renders the comparison side by side without a combined score", () => {
    const html = renderToStaticMarkup(<ResultsTable result={comparisonResult} />);
    expect(html).toContain("Departures per day");
    expect(html).toContain("594.4");
    expect(html).toContain("Taxi-out excess");
    expect(html).toContain("n = 216,100");
    expect(html).toContain("Documented constraints (context)");
    expect(html).not.toMatch(/combined score[^<]*:/i);
  });

  it("renders the long-haul share with both threshold units and the excluded share", () => {
    const html = renderToStaticMarkup(<ResultsTable result={longHaulResult} />);
    expect(html).toContain("12.1%");
    expect(html).toContain("3,452 mi");
    expect(html).toContain("3,000 nmi");
    expect(html).toContain("0.5% of all departures");
    expect(html).toContain('class="bar__fill" style="width:59.32%"');
  });

  it("marks a partial unmet-demand result and shows its conditions", () => {
    const html = renderToStaticMarkup(<ResultsTable result={unmetDemandResult} />);
    expect(html).toContain("Partial result");
    expect(html).toContain("Signs of constrained demand");
    expect(html).toContain(">Met<");
    expect(html).toContain("+4.1%");
    expect(html).toContain("Monthly delay causes at arrival, CY2025");
  });
});

describe("Message", () => {
  it("shows the answer, then results, then limitations, then sources", () => {
    const item: AssistantItem = { kind: "assistant", key: "a:1", content: "", response: sampleResponses.comparison };
    const html = renderToStaticMarkup(<Message item={item} canRetry={false} onRetry={noop} onStartNewConversation={noop} />);
    const positions = ["LAX shows more", "Congestion comparison", "Limitations</p>", "Sources (2)"].map((text) => html.indexOf(text));
    expect(positions.every((position) => position >= 0)).toBe(true);
    expect([...positions].sort((a, b) => a - b)).toEqual(positions);
  });

  it("offers Retry for a failed request only when allowed", () => {
    const item: UserItem = {
      kind: "user",
      key: "u:1",
      content: "Compare LAX and SNA",
      request: {
        requestId: "1",
        sentConversationId: null,
        phase: "interrupted",
        registered: true,
        retry: "same",
        error: null,
        startedAt: null,
      },
    };
    const allowed = renderToStaticMarkup(<Message item={item} canRetry onRetry={noop} onStartNewConversation={noop} />);
    expect(allowed).toContain("Processing was interrupted before an answer was saved.");
    expect(allowed).toContain(">Retry</button>");
    const blocked = renderToStaticMarkup(<Message item={item} canRetry={false} onRetry={noop} onStartNewConversation={noop} />);
    expect(blocked).not.toContain(">Retry</button>");
  });
});

describe("Login", () => {
  it("offers a sign-up link to visitors without an account", () => {
    const api = createApiClient(async () => new Response(null, { status: 204 }));
    const html = renderToStaticMarkup(<Login api={api} notice={null} onSignedIn={noop} />);
    expect(html).toContain("Don&#x27;t have an account yet?");
    expect(html).toContain(">Sign up</button>");
    expect(html).not.toContain("Confirm password");
  });
});
