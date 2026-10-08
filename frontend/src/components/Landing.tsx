import type { ReactNode } from "react";

interface Props {
  themeToggle?: ReactNode;
  onSignIn: () => void;
  onSignUp: () => void;
}

const STEPS = [
  { n: "1", title: "Describe it", text: "Tell the design chat what you are building. It asks a few multiple-choice questions and records your answers as requirements." },
  { n: "2", title: "Pick an architecture", text: "Compare generated options, then refine on a live canvas: drag components, connect them, or type “add Redis between Orders and its DB”." },
  { n: "3", title: "Check it", text: "Health checks flag missing pieces, and every requirement is traced to the components that satisfy it." },
  { n: "4", title: "Ship it", text: "Turn components into OpenSpec change specs for coding agents, and compare the design with your real repository." },
];

const FEATURES: { icon: string; title: string; text: string }[] = [
  { icon: "✦", title: "AI design chat", text: "From a one-line idea to a reviewed architecture, with the reasoning saved to project memory." },
  { icon: "◎", title: "Live collaboration", text: "Edit the same canvas together with presence and instant sync. Edits made offline sync when you reconnect." },
  { icon: "⎇", title: "Branches & versions", text: "Try ideas on a branch, diff versions, restore any commit and adopt a design through a recorded decision." },
  { icon: "✓", title: "Health & traceability", text: "Rules catch single points of failure, missing auth and orphan components, and show which requirements each part covers." },
  { icon: "⚙", title: "Vibe code with OpenSpec", text: "Select components and generate a structured change spec that Claude Code, Codex or Cursor can implement." },
  { icon: "⟨/⟩", title: "Code analysis", text: "Reconstruct a repository's architecture, detect drift from the design and estimate the impact of a change." },
  { icon: "⌘", title: "MCP for agents", text: "Connect Claude, Codex and other agents so they can read and update the architecture over MCP." },
  { icon: "⚡", title: "Bring your own model", text: "Gemini, Claude, OpenAI, OpenRouter, Mistral, Groq, Azure or a self-hosted model, with fallbacks and monthly budgets." },
  { icon: "⇄", title: "Convert & export", text: "Swap a design between AWS, Azure, Google Cloud and open source, then export PNG, SVG or an interactive HTML diagram." },
];

/** A small, static architecture sketch for the hero. */
function HeroDiagram() {
  const boxes = [
    { x: 20, y: 30, w: 120, label: "Web & WhatsApp", kind: "client" },
    { x: 200, y: 30, w: 120, label: "API Gateway", kind: "edge" },
    { x: 380, y: 30, w: 120, label: "Support Agent", kind: "ai" },
    { x: 200, y: 150, w: 120, label: "Orders Service", kind: "svc" },
    { x: 380, y: 150, w: 120, label: "MCP Tools", kind: "ai" },
    { x: 200, y: 260, w: 120, label: "Orders DB", kind: "db" },
    { x: 380, y: 260, w: 120, label: "Redis Cache", kind: "db" },
  ];
  const lines = [[140, 52, 200, 52], [320, 52, 380, 52], [260, 74, 260, 150], [440, 74, 440, 150], [260, 194, 260, 260],
    [320, 172, 380, 172], [320, 282, 380, 282]];
  return (
    <svg className="landing-diagram" viewBox="0 0 520 320" role="img" aria-label="Example architecture on the ArchFlow canvas">
      <defs>
        <pattern id="lgrid" width="16" height="16" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" className="lgrid-dot" /></pattern>
      </defs>
      <rect width="520" height="320" rx="14" fill="url(#lgrid)" />
      {lines.map(([x1, y1, x2, y2], i) => <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} className="lline" />)}
      {boxes.map((b) => (
        <g key={b.label}>
          <rect x={b.x} y={b.y} width={b.w} height="44" rx="8" className={`lbox ${b.kind}`} />
          <rect x={b.x} y={b.y} width="4" height="44" rx="2" className={`lbar ${b.kind}`} />
          <text x={b.x + 14} y={b.y + 27} className="ltext">{b.label}</text>
        </g>
      ))}
      <g className="lchip">
        <rect x="20" y="150" width="140" height="58" rx="10" />
        <text x="32" y="172" className="lchip-t">✓ Health 98%</text>
        <text x="32" y="192" className="lchip-s">17 requirements traced</text>
      </g>
    </svg>
  );
}

/** Public landing page, shown at / to signed-out visitors. */
export function Landing({ themeToggle, onSignIn, onSignUp }: Props) {
  return (
    <div className="landing">
      <header className="landing-bar">
        <div className="home-brand"><span className="logo">▲</span> ArchFlow</div>
        <nav className="landing-nav">
          <a href="#how" onClick={(e) => { e.preventDefault(); document.getElementById("how")?.scrollIntoView({ behavior: "smooth" }); }}>How it works</a>
          <a href="#features" onClick={(e) => { e.preventDefault(); document.getElementById("features")?.scrollIntoView({ behavior: "smooth" }); }}>Features</a>
        </nav>
        <span className="spacer" />
        {themeToggle}
        <button className="ghost" onClick={onSignIn}>Sign in</button>
        <button className="primary" onClick={onSignUp}>Get started</button>
      </header>

      <section className="landing-hero">
        <div className="landing-copy">
          <span className="landing-eyebrow">Architecture design, with AI and your team</span>
          <h1>Design systems as one living model, from requirements to code.</h1>
          <p className="landing-lead">ArchFlow turns an idea into requirements, an architecture and a live canvas your whole team and AI agents
            edit together, then into change specs your coding agents can build.</p>
          <div className="row gap landing-cta">
            <button className="primary big" onClick={onSignUp}>Create a free account</button>
            <button className="big" onClick={onSignIn}>Sign in</button>
          </div>
          <p className="muted small">Self-hosted · bring your own AI model · works without AI too</p>
        </div>
        <div className="landing-visual"><HeroDiagram /></div>
      </section>

      <section id="how" className="landing-section">
        <h2>How it works</h2>
        <div className="landing-steps">
          {STEPS.map((s) => (
            <div key={s.n} className="landing-step">
              <span className="landing-step-n">{s.n}</span>
              <b>{s.title}</b>
              <p className="muted small">{s.text}</p>
            </div>
          ))}
        </div>
      </section>

      <section id="features" className="landing-section">
        <h2>Everything in one place</h2>
        <p className="muted landing-sub">No more diagrams that drift from the docs, or docs that drift from the code.</p>
        <div className="landing-features">
          {FEATURES.map((f) => (
            <div key={f.title} className="landing-feature">
              <span className="landing-icon" aria-hidden>{f.icon}</span>
              <b>{f.title}</b>
              <p className="muted small">{f.text}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="landing-final">
        <h2>Start with an idea. Leave with a design you can build.</h2>
        <p className="muted">Create an account, then start from a blank canvas or the AI customer-support example.</p>
        <button className="primary big" onClick={onSignUp}>Get started</button>
      </section>

      <footer className="landing-foot">
        <span className="home-brand small"><span className="logo">▲</span> ArchFlow</span>
        <span className="muted small">Requirements → Architecture → Canvas → Code</span>
      </footer>
    </div>
  );
}
