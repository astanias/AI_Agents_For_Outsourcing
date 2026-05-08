import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import {
  getGroup,
  getGroupAvailability,
  getGroupMembers,
  type GroupAvailabilitySlot,
  type GroupMember,
  type GroupSummary,
} from "./groups.api";
import TeamMeetingPlanner from "./TeamMeetingPlanner";
import GroupAvailabilityCalendar from "./GroupAvailabilityCalendar";

function formatGroupToken(groupId: number) {
  return String(groupId).padStart(9, "0");
}

function displayName(member: Pick<GroupMember, "first_name" | "last_name">) {
  return `${member.first_name} ${member.last_name}`.trim();
}

export default function GroupDetail() {
  const { groupId } = useParams();
  const navigate = useNavigate();

  const parsedGroupId = Number(groupId);

  const [group, setGroup] = useState<GroupSummary | null>(null);
  const [members, setMembers] = useState<GroupMember[]>([]);
  const [availabilitySlots, setAvailabilitySlots] = useState<GroupAvailabilitySlot[]>([]);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const memberCount = members.length;

  const roleLabel = useMemo(() => {
    const role = group?.role ?? "member";
    if (role === "owner") return "Owner";
    if (role === "admin") return "Manager";
    if (role === "member") return "Member";
    return role;
  }, [group?.role]);

  useEffect(() => {
    if (!Number.isFinite(parsedGroupId) || parsedGroupId <= 0) {
      setError("Invalid group id.");
      setLoading(false);
      return;
    }

    let cancelled = false;

    async function load() {
      setLoading(true);
      setError("");

      try {
        const [groupResponse, memberResponse, availabilityResponse] = await Promise.all([
          getGroup(parsedGroupId),
          getGroupMembers(parsedGroupId),
          getGroupAvailability(parsedGroupId),
        ]);

        if (cancelled) return;
        setGroup(groupResponse);
        setMembers(memberResponse);
        setAvailabilitySlots(availabilityResponse);
      } catch (err) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : "Failed to load group.";
        if (message.toLowerCase().includes("unauthorized") || message.toLowerCase().includes("not authenticated")) {
          localStorage.removeItem("access_token");
          navigate("/login");
          return;
        }
        setError(message);
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    load();

    return () => {
      cancelled = true;
    };
  }, [navigate, parsedGroupId]);

  if (loading) return <div className="p-8 text-slate-400">Loading group workspace...</div>;
  if (error) return <div className="p-8 text-red-400">{error}</div>;

  if (!group) {
    return <div className="p-8 text-slate-400">Group not found.</div>;
  }

  return (
    <div className="max-w-6xl mx-auto space-y-8">
      <header className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div>
          <p className="text-sm text-slate-500 dark:text-slate-400">Team Workspace</p>
          <h1 className="text-3xl font-bold text-slate-900 dark:text-white">{group.name}</h1>
          <p className="mt-2 text-slate-600 dark:text-slate-400">
            {group.description || "No description provided yet."}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Link
            to="/groups"
            className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm transition hover:bg-slate-50 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
          >
            Back to groups
          </Link>
        </div>
      </header>

      <section className="grid gap-4 md:grid-cols-2">
        <article className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Join token</p>
          <p className="mt-2 font-mono text-lg tracking-[0.35em] text-slate-900 dark:text-white">
            {formatGroupToken(group.id)}
          </p>
          <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
            Share this 9-digit token to let someone join.
          </p>
        </article>

        <article className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-800 dark:bg-slate-900">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Your role</p>
              <p className="mt-2 text-lg font-semibold text-slate-900 dark:text-white">{roleLabel}</p>
            </div>
            <div className="text-right">
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Roster</p>
              <p className="mt-2 text-lg font-semibold text-slate-900 dark:text-white">{memberCount}</p>
            </div>
          </div>
        </article>
      </section>

      <div className="grid gap-8 lg:grid-cols-3">
        <div className="lg:col-span-2 space-y-8">
          <TeamMeetingPlanner members={members} availabilitySlots={availabilitySlots} />

          <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-xl font-semibold text-slate-900 dark:text-white">Roster</h2>
                <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                  {members.length} teammate{members.length === 1 ? "" : "s"} in this group.
                </p>
              </div>
            </div>

            {members.length === 0 ? (
              <p className="mt-4 text-slate-400">No members found.</p>
            ) : (
              <div className="mt-5 grid gap-3 sm:grid-cols-2">
                {members.map((member) => (
                  <div
                    key={member.id}
                    className="rounded-xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-800 dark:bg-slate-800/40"
                  >
                    <p className="font-semibold text-slate-900 dark:text-white">{displayName(member)}</p>
                    <p className="mt-1 text-sm text-slate-500 dark:text-slate-400 truncate">{member.email}</p>
                    <span className="mt-3 inline-flex items-center rounded-full bg-blue-100 px-2.5 py-1 text-xs font-medium text-blue-800 dark:bg-blue-900/30 dark:text-blue-300 capitalize">
                      {member.role}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>

        <div className="space-y-8">
          <GroupAvailabilityCalendar members={members} slots={availabilitySlots} />
        </div>
      </div>
    </div>
  );
}
