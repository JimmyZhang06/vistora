export function PageHeading({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="page-heading">
      <div className="page-heading-copy">
        <p className="eyebrow">{eyebrow}</p>
        <h1 tabIndex={-1}>{title}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "accent" | "success" | "warning";
}) {
  const modifier = tone === "neutral" ? "" : ` badge--${tone}`;
  return <span className={`badge${modifier}`}>{children}</span>;
}

export function StatePanel({
  code,
  title,
  description,
  children,
  error = false,
}: {
  code: string;
  title: string;
  description: string;
  children?: React.ReactNode;
  error?: boolean;
}) {
  const codeShape = code.length > 3 ? "pill" : "circle";

  return (
    <section className="state-panel" role={error ? "alert" : undefined}>
      <div>
        <span className="state-code" data-shape={codeShape} aria-hidden="true">{code}</span>
        <h2>{title}</h2>
        <p>{description}</p>
        {children ? <div className="button-row" style={{ justifyContent: "center" }}>{children}</div> : null}
      </div>
    </section>
  );
}
