import { useState } from "react";
import Assistant from "../../components/Assistant";
import { PageHeader, TabPanel, Tabs } from "../../components/ui";
import { hasCompany, useMe } from "../../hooks";
import { Departments } from "./Departments";
import { Members } from "./People";
import { AuditLog, DataRetention, Settings, Usage } from "./Sections";
import Models from "./Models";
import OverviewTab from "./Overview";

export default function OwnerConsole() {
  const { me } = useMe();
  const tabs: [string, string, string][] = [
    ["overview", "Overview", "report.executive.read"],
    ["briefing", "AI briefing", "report.executive.read"],
    ["departments", "Departments", "department.manage"],
    ["members", "People & roles", "member.manage"],
    ["models", "AI models", "model.manage"],
    ["usage", "AI usage", "ai.usage.read"],
    ["data", "Data & retention", "company.settings.manage"],
    ["audit", "Audit log", "audit.read"],
    ["settings", "Settings", "company.settings.manage"],
  ];
  const visible = tabs.filter(([, , perm]) => hasCompany(me, perm));
  const [tab, setTab] = useState(visible[0]?.[0] ?? "overview");

  return (
    <div>
      <PageHeader crumbs={[{ label: "Home", to: "/" }, { label: "Owner console" }]} title="Owner console"
                  description={`Run ${me.company_name}: decisions, departments, people, AI models, data protection and audit.`} />
      <Tabs id="owner" tabs={visible.map(([key, label]) => [key, label])} active={tab} onChange={setTab} />
      <TabPanel id="owner" active={tab}>
      {tab === "overview" && <OverviewTab go={setTab} />}
      {tab === "briefing" && (
        <Assistant departmentId={null} examples={["What needs my attention today?",
                                                   "Which projects are blocked and which departments are close to budget?"]} />
      )}
      {tab === "departments" && <Departments />}
      {tab === "members" && <Members />}
      {tab === "models" && <Models />}
      {tab === "usage" && <Usage />}
      {tab === "data" && <DataRetention />}
      {tab === "audit" && <AuditLog />}
      {tab === "settings" && <Settings />}
      </TabPanel>
    </div>
  );
}
