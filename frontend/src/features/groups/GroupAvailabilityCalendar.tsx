import { useMemo, useState } from "react";

import type { GroupAvailabilitySlot, GroupMember } from "./groups.api";

interface Props {
  members: GroupMember[];
  slots: GroupAvailabilitySlot[];
}

const DAY_LABELS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function formatTime(value: string) {
  return value.slice(0, 5);
}

function displayName(member: Pick<GroupMember, "first_name" | "last_name">) {
  return `${member.first_name} ${member.last_name}`.trim();
}

export default function GroupAvailabilityCalendar({ members, slots }: Props) {
  const [selectedUserId, setSelectedUserId] = useState<number>(0);

  const filteredSlots = useMemo(() => {
    if (selectedUserId === 0) return slots;
    return slots.filter((slot) => slot.user_id === selectedUserId);
  }, [slots, selectedUserId]);

  const slotsByDay = useMemo(() => {
    const map = new Map<number, GroupAvailabilitySlot[]>();
    for (const slot of filteredSlots) {
      const current = map.get(slot.day_of_week) ?? [];
      current.push(slot);
      map.set(slot.day_of_week, current);
    }
    return map;
  }, [filteredSlots]);

  const memberById = useMemo(() => {
    const map = new Map<number, GroupMember>();
    for (const member of members) {
      map.set(member.id, member);
    }
    return map;
  }, [members]);

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-slate-900 dark:text-white">Group availability</h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Weekly availability windows saved by teammates.
          </p>
        </div>

        <select
          value={selectedUserId}
          onChange={(event) => setSelectedUserId(Number(event.target.value))}
          className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200"
        >
          <option value={0}>All members</option>
          {members.map((member) => (
            <option key={member.id} value={member.id}>
              {displayName(member)}
            </option>
          ))}
        </select>
      </div>

      <div className="mt-5 space-y-4">
        {DAY_LABELS.map((label, dayIndex) => {
          const daySlots = slotsByDay.get(dayIndex) ?? [];
          return (
            <div key={label} className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-semibold text-slate-800 dark:text-slate-200">{label}</h3>
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  {daySlots.length} window{daySlots.length === 1 ? "" : "s"}
                </span>
              </div>

              {daySlots.length === 0 ? (
                <p className="mt-2 text-sm text-slate-400">No availability saved.</p>
              ) : (
                <div className="mt-3 grid gap-2">
                  {daySlots.map((slot) => {
                    const member = memberById.get(slot.user_id);
                    const name = member ? displayName(member) : `${slot.first_name} ${slot.last_name}`;
                    return (
                      <div
                        key={slot.id}
                        className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-700 dark:bg-slate-800/50 dark:text-slate-200"
                      >
                        <span className="font-medium">{name}</span>
                        <span className="font-mono text-xs text-slate-500 dark:text-slate-400">
                          {formatTime(slot.start_time)} - {formatTime(slot.end_time)}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
