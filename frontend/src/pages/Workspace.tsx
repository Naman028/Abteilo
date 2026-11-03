import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import Assistant from "../components/Assistant";
import { DEPARTMENT_PURPOSE, PageHeader, TabPanel, Tabs } from "../components/ui";
import { useMe } from "../hooks";
import Documents from "./panels/Documents";
import Engineering from "./panels/Engineering";
import Finance from "./panels/Finance";
import HR from "./panels/HR";
import IT from "./panels/IT";

const EXAMPLES: Record<string, string[]> = {
  engineering: ["What is the status of project A17?", "Which tasks are blocked?"],
  finance: ["How much budget is left this quarter?", "Which purchase requests are pending?"],
  human_resources: ["How many leave days do I have left?", "What does the leave policy say about carry-over?"],
  it_security: ["Which access requests are open?", "Who approves role changes?"],
  custom: ["Summarise our department documents."],
};

export default function WorkspacePage() {
  const { departmentId } = useParams();
  const { me } = useMe();
  const ws = me.workspaces.find((w) => w.department_id === departmentId);
  const [tab, setTab] = useState("assistant");
  useEffect(() => {
    setTab("assistant");
  }, [departmentId]);

  if (!ws) return <div className="card empty">This workspace is not available to you. Ask the owner for access.</div>;
  const can = (perm: string) => ws.permissions.includes(perm);

  const tabs: [string, string, boolean][] = [
    ["assistant", "Assistant", ws.modules.includes("assistant")],
    ["projects", "Projects", ws.modules.includes("projects") && can("engineering.project.read")],
    ["finance", "Budgets & approvals", ws.modules.includes("finance") && can("finance.budget.read")],
    ["hr", "Leave management", ws.modules.includes("hr") && can("hr.leave.approve")],
    ["it", "Access management", ws.modules.includes("it") && (can("it.access.propose") || me.company_permissions.includes("it.access.approve"))],
    ["documents", "Documents", ws.modules.includes("documents")],
  ];

  return (
    <div>
      <PageHeader crumbs={[{ label: "Home", to: "/" }, { label: "Departments" }, { label: ws.name }]} title={ws.name}
                  description={DEPARTMENT_PURPOSE[ws.template] ?? DEPARTMENT_PURPOSE.custom}
                  meta={`Your roles here grant ${ws.permissions.length} permission${ws.permissions.length === 1 ? "" : "s"}.`} />
      <Tabs id="ws" tabs={tabs.filter(([, , show]) => show).map(([key, label]) => [key, label])} active={tab} onChange={setTab} />
      <TabPanel id="ws" active={tab}>
      {tab === "assistant" && <Assistant key={ws.department_id} departmentId={ws.department_id}
                                          examples={EXAMPLES[ws.template] ?? EXAMPLES.custom} />}
      {tab === "projects" && <Engineering ws={ws} />}
      {tab === "finance" && <Finance ws={ws} />}
      {tab === "hr" && <HR ws={ws} />}
      {tab === "it" && <IT ws={ws} />}
      {tab === "documents" && <Documents ws={ws} />}
      </TabPanel>
    </div>
  );
}
