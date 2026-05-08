import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  createMeeting,
  fetchMeetingRecommendations,
  type MeetingRecommendation,
} from "../../services/meetingsApi";
import type { GroupAvailabilitySlot, GroupMember } from "./groups.api";

interface Props {
  members: GroupMember[];
  availabilitySlots: GroupAvailabilitySlot[];
  defaultTitle?: string;
  onCreated?: () => void;
}

type PlacedMeeting = {
  date: string;
  dayOfWeek: number;
  startMinutes: number;
  endMinutes: number;
};

const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const HOURS = Array.from({ length: 16 }, (_, index) => index + 7);
const DAY_START_MINUTES = 7 * 60;
const DAY_END_MINUTES = 23 * 60;
const STEP_MINUTES = 15;
const DEFAULT_COLOR = "#2563eb";

function parseMinutes(value: string) {
  const [hour, minute] = value.split(":").map(Number);
  return hour * 60 + minute;
}

function formatTimeLabel(minutes: number) {
  const hour24 = Math.floor(minutes / 60);
  const minute = minutes % 60;
  const hour12 = hour24 % 12 === 0 ? 12 : hour24 % 12;
  const suffix = hour24 < 12 ? "AM" : "PM";
  return `${hour12}:${String(minute).padStart(2, "0")} ${suffix}`;
}

function formatDateInput(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function buildWeekStart(dateValue: string) {
  const date = new Date(`${dateValue}T12:00:00`);
  const dayOffset = date.getDay();
  date.setDate(date.getDate() - dayOffset);
  date.setHours(0, 0, 0, 0);
  return date;
}

function addDays(date: Date, days: number) {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function snapToStep(minutes: number) {
  return Math.round(minutes / STEP_MINUTES) * STEP_MINUTES;
}

function getDurationMinutes(startTime: string, endTime: string) {
  return parseMinutes(endTime) - parseMinutes(startTime);
}

function dedupeEmails(emails: string[]) {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const email of emails) {
    const normalized = email.trim().toLowerCase();
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    result.push(normalized);
  }
  return result;
}

function memberDisplayName(member: Pick<GroupMember, "first_name" | "last_name">) {
  return `${member.first_name} ${member.last_name}`.trim();
}

function getSelectedMembers(members: GroupMember[], selectedMemberIds: Set<number>) {
  return members.filter((member) => selectedMemberIds.has(member.id));
}

function getAvailabilityBlocks(
  selectedMembers: GroupMember[],
  slots: GroupAvailabilitySlot[],
  dayOfWeek: number,
  startMinutes: number,
  endMinutes: number,
) {
  const unavailableMembers = selectedMembers.filter((member) => {
    const memberSlots = slots.filter(
      (slot) => slot.user_id === member.id && slot.day_of_week === dayOfWeek,
    );
    return !memberSlots.some((slot) => {
      const slotStart = parseMinutes(slot.start_time);
      const slotEnd = parseMinutes(slot.end_time);
      return startMinutes >= slotStart && endMinutes <= slotEnd;
    });
  });

  if (unavailableMembers.length === 0) {
    return { status: "full" as const, unavailableMembers };
  }
  if (unavailableMembers.length === selectedMembers.length) {
    return { status: "none" as const, unavailableMembers };
  }
  return { status: "partial" as const, unavailableMembers };
}

function timeChunkTop(startMinutes: number, hour: number) {
  const hourStart = hour * 60;
  return ((startMinutes - hourStart) / 60) * 100;
}

function timeChunkHeight(startMinutes: number, endMinutes: number, hour: number) {
  const hourStart = hour * 60;
  const hourEnd = hourStart + 60;
  const chunkStart = Math.max(startMinutes, hourStart);
  const chunkEnd = Math.min(endMinutes, hourEnd);
  return ((chunkEnd - chunkStart) / 60) * 100;
}

export default function TeamMeetingPlanner({
  members,
  availabilitySlots,
  defaultTitle = "Team sync",
  onCreated,
}: Props) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [slotError, setSlotError] = useState("");
  const [success, setSuccess] = useState("");

  const [title, setTitle] = useState(defaultTitle);
  const [description, setDescription] = useState("");
  const [location, setLocation] = useState("");
  const [meetingType, setMeetingType] = useState<"in_person" | "virtual">("in_person");
  const [date, setDate] = useState("");
  const [startTime, setStartTime] = useState("09:00");
  const [endTime, setEndTime] = useState("10:00");
  const [durationMinutes, setDurationMinutes] = useState(60);
  const [selectedMemberIds, setSelectedMemberIds] = useState<Set<number>>(new Set());
  const [additionalEmails, setAdditionalEmails] = useState("");
  const [recommendedSlots, setRecommendedSlots] = useState<MeetingRecommendation[] | null>(null);
  const [loadingSlots, setLoadingSlots] = useState(false);
  const [placedMeeting, setPlacedMeeting] = useState<PlacedMeeting | null>(null);
  const [dragPreview, setDragPreview] = useState<PlacedMeeting | null>(null);
  const [draggingBlock, setDraggingBlock] = useState(false);

  useEffect(() => {
    const now = new Date();
    setDate(formatDateInput(now));
  }, []);

  useEffect(() => {
    setSelectedMemberIds(new Set(members.map((member) => member.id)));
  }, [members]);

  useEffect(() => {
    const minutes = getDurationMinutes(startTime, endTime);
    if (minutes > 0) {
      setDurationMinutes(minutes);
    }
  }, [startTime, endTime]);

  const selectedMembers = useMemo(
    () => getSelectedMembers(members, selectedMemberIds),
    [members, selectedMemberIds],
  );

  const attendeeEmails = useMemo(() => {
    const selectedEmails = selectedMembers.map((member) => member.email);
    const extraEmails = additionalEmails
      .split(",")
      .map((email) => email.trim())
      .filter(Boolean);
    return dedupeEmails([...selectedEmails, ...extraEmails]);
  }, [selectedMembers, additionalEmails]);

  const weekStart = useMemo(() => buildWeekStart(date), [date]);
  const weekDays = useMemo(
    () => Array.from({ length: 7 }, (_, index) => {
      const dayDate = addDays(weekStart, index);
      return {
        index,
        date: formatDateInput(dayDate),
        label: `${WEEKDAY_LABELS[index]} ${String(dayDate.getMonth() + 1).padStart(2, "0")}/${String(dayDate.getDate()).padStart(2, "0")}`,
      };
    }),
    [weekStart],
  );

  function updatePlacement(dayOfWeek: number, hour: number, clientY?: number, cellHeight = 60) {
    const minuteOffset = clientY === undefined ? 0 : clamp(Math.floor((clientY / cellHeight) * 60), 0, 59);
    const rawStart = hour * 60 + minuteOffset;
    const maxStart = DAY_END_MINUTES - durationMinutes;
    const startMinutes = clamp(snapToStep(rawStart), DAY_START_MINUTES, maxStart);
    const endMinutes = startMinutes + durationMinutes;
    const actualDate = addDays(weekStart, dayOfWeek);

    const placement: PlacedMeeting = {
      date: formatDateInput(actualDate),
      dayOfWeek,
      startMinutes,
      endMinutes,
    };

    setPlacedMeeting(placement);
    setStartTime(`${String(Math.floor(startMinutes / 60)).padStart(2, "0")}:${String(startMinutes % 60).padStart(2, "0")}`);
    setEndTime(`${String(Math.floor(endMinutes / 60)).padStart(2, "0")}:${String(endMinutes % 60).padStart(2, "0")}`);
  }

  async function handleCreateMeeting(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSuccess("");

    if (!title.trim()) {
      setError("Please provide a title.");
      return;
    }

    if (!placedMeeting) {
      setError("Drag the meeting block onto the calendar first.");
      return;
    }

    if (placedMeeting.endMinutes <= placedMeeting.startMinutes) {
      setError("Meeting end time must be after the start time.");
      return;
    }

    if (attendeeEmails.length === 0) {
      setError("Pick at least one teammate or add an email.");
      return;
    }

    setSaving(true);

    try {
      const startDateTime = `${placedMeeting.date}T${String(Math.floor(placedMeeting.startMinutes / 60)).padStart(2, "0")}:${String(placedMeeting.startMinutes % 60).padStart(2, "0")}:00`;
      const endDateTime = `${placedMeeting.date}T${String(Math.floor(placedMeeting.endMinutes / 60)).padStart(2, "0")}:${String(placedMeeting.endMinutes % 60).padStart(2, "0")}:00`;

      await createMeeting({
        title: title.trim(),
        description: description.trim() || undefined,
        location: location.trim() || undefined,
        meeting_type: meetingType,
        color: DEFAULT_COLOR,
        start_time: startDateTime,
        end_time: endDateTime,
        attendee_emails: attendeeEmails,
      });

      setSuccess("Meeting created and invites sent.");
      setRecommendedSlots(null);
      setError("");
      onCreated?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create meeting.");
    } finally {
      setSaving(false);
    }
  }

  const fetchRecommendedSlots = async () => {
    setSlotError("");
    setRecommendedSlots(null);

    if (!date) {
      setSlotError("Pick a date first.");
      return;
    }

    if (durationMinutes <= 0) {
      setSlotError("Set an end time that is after the start time.");
      return;
    }

    if (attendeeEmails.length === 0) {
      setSlotError("Pick at least one teammate or add an email.");
      return;
    }

    setLoadingSlots(true);

    try {
      const response = await fetchMeetingRecommendations({
        attendee_emails: attendeeEmails,
        start_date: date,
        end_date: date,
        duration_minutes: durationMinutes,
        max_results: 3,
        include_organizer: true,
      });

      setRecommendedSlots(response.recommendations);
    } catch (err) {
      setRecommendedSlots([]);
      setSlotError(err instanceof Error ? err.message : "Failed to find recommended time slots.");
    } finally {
      setLoadingSlots(false);
    }
  };

  const handleSelectSlot = (slot: MeetingRecommendation) => {
    const nextStart = extractTime(slot.start_time);
    const nextEnd = extractTime(slot.end_time);
    setStartTime(nextStart);
    setEndTime(nextEnd);
    setDurationMinutes(getDurationMinutes(nextStart, nextEnd));
    setPlacedMeeting({
      date,
      dayOfWeek: new Date(`${date}T12:00:00`).getDay(),
      startMinutes: parseMinutes(nextStart),
      endMinutes: parseMinutes(nextEnd),
    });
  };

  function extractTime(value: string) {
    const [, timePart = ""] = value.split("T");
    return timePart.slice(0, 5);
  }

  const allSelected = selectedMemberIds.size > 0 && selectedMemberIds.size === members.length;

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <div className="mb-5 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-slate-900 dark:text-white">Make a meeting</h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Drag the meeting block onto the calendar to snap it into a time slot.
          </p>
        </div>
        <span className="rounded-full bg-blue-100 px-3 py-1 text-xs font-semibold text-blue-800 dark:bg-blue-900/30 dark:text-blue-300">
          {members.length} members
        </span>
      </div>

      <form onSubmit={handleCreateMeeting} className="grid gap-6 xl:grid-cols-[360px_1fr]">
        <div className="grid gap-4">
          <div className="flex rounded-lg border border-slate-200 overflow-hidden dark:border-slate-700">
            <button
              type="button"
              onClick={() => {
                setMeetingType("in_person");
                setLocation("");
              }}
              className={`flex-1 px-4 py-2.5 text-sm font-medium transition-colors ${
                meetingType === "in_person"
                  ? "bg-blue-600 text-white"
                  : "text-slate-500 hover:bg-slate-50 dark:text-slate-400 dark:hover:bg-slate-800"
              }`}
            >
              In person
            </button>
            <button
              type="button"
              onClick={() => {
                setMeetingType("virtual");
                setLocation("");
              }}
              className={`flex-1 px-4 py-2.5 text-sm font-medium transition-colors ${
                meetingType === "virtual"
                  ? "bg-slate-800 text-white dark:bg-slate-700"
                  : "text-slate-500 hover:bg-slate-50 dark:text-slate-400 dark:hover:bg-slate-800"
              }`}
            >
              Virtual
            </button>
          </div>

          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Meeting title"
            className="rounded-lg border border-slate-200 px-4 py-3 text-slate-900 outline-none transition focus:border-blue-500 dark:border-slate-700 dark:bg-slate-950 dark:text-white"
          />

          <input
            value={location}
            onChange={(event) => setLocation(event.target.value)}
            placeholder={meetingType === "in_person" ? "Address or room" : "Meeting link (Zoom, Teams, etc.)"}
            className="rounded-lg border border-slate-200 px-4 py-3 text-slate-900 outline-none transition focus:border-blue-500 dark:border-slate-700 dark:bg-slate-950 dark:text-white"
          />

          <textarea
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="Description"
            rows={3}
            className="rounded-lg border border-slate-200 px-4 py-3 text-slate-900 outline-none transition focus:border-blue-500 dark:border-slate-700 dark:bg-slate-950 dark:text-white"
          />

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="grid gap-1">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Date</span>
              <input
                type="date"
                value={date}
                onChange={(event) => {
                  setDate(event.target.value);
                  setRecommendedSlots(null);
                  setSlotError("");
                  setPlacedMeeting(null);
                }}
                className="rounded-lg border border-slate-200 px-4 py-3 text-slate-900 outline-none transition focus:border-blue-500 dark:border-slate-700 dark:bg-slate-950 dark:text-white [color-scheme:dark]"
              />
            </label>
            <label className="grid gap-1">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">Duration</span>
              <select
                value={durationMinutes}
                onChange={(event) => {
                  const nextDuration = Number(event.target.value);
                  setDurationMinutes(nextDuration);
                  setEndTime(
                    `${String(Math.floor((parseMinutes(startTime) + nextDuration) / 60)).padStart(2, "0")}:${String((parseMinutes(startTime) + nextDuration) % 60).padStart(2, "0")}`,
                  );
                  setPlacedMeeting(null);
                }}
                className="rounded-lg border border-slate-200 px-4 py-3 text-slate-900 outline-none transition focus:border-blue-500 dark:border-slate-700 dark:bg-slate-950 dark:text-white"
              >
                {[30, 45, 60, 90, 120].map((value) => (
                  <option key={value} value={value}>
                    {value} minutes
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-800/50">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium text-slate-800 dark:text-slate-200">Invite teammates</p>
                <p className="text-xs text-slate-500 dark:text-slate-400">Select who should get the invite.</p>
              </div>
              <button
                type="button"
                onClick={() => {
                  setSelectedMemberIds(allSelected ? new Set() : new Set(members.map((member) => member.id)));
                }}
                className="text-sm font-medium text-blue-600 hover:underline dark:text-blue-400"
              >
                {allSelected ? "Clear" : "Select all"}
              </button>
            </div>

            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {members.map((member) => {
                const checked = selectedMemberIds.has(member.id);
                return (
                  <label
                    key={member.id}
                    className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(event) => {
                        setSelectedMemberIds((prev) => {
                          const next = new Set(prev);
                          if (event.target.checked) {
                            next.add(member.id);
                          } else {
                            next.delete(member.id);
                          }
                          return next;
                        });
                        setRecommendedSlots(null);
                        setSlotError("");
                      }}
                      className="h-4 w-4"
                    />
                    <span className="truncate">
                      {member.first_name} {member.last_name}
                    </span>
                    <span className="ml-auto truncate text-xs text-slate-400">{member.email}</span>
                  </label>
                );
              })}
            </div>

            <input
              value={additionalEmails}
              onChange={(event) => {
                setAdditionalEmails(event.target.value);
                setRecommendedSlots(null);
                setSlotError("");
              }}
              placeholder="Additional attendee emails (comma-separated)"
              className="mt-3 w-full rounded-lg border border-slate-200 bg-white px-4 py-3 text-slate-900 outline-none focus:border-blue-500 dark:border-slate-700 dark:bg-slate-950 dark:text-white"
            />
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm font-medium text-slate-700 dark:text-slate-300">Recommended times</span>
              <button
                type="button"
                onClick={fetchRecommendedSlots}
                disabled={loadingSlots}
                className="text-sm font-medium text-blue-600 hover:underline disabled:opacity-50 dark:text-blue-400"
              >
                {loadingSlots ? "Searching..." : "Find slots"}
              </button>
            </div>

            {slotError ? <p className="mt-2 text-xs text-rose-500">{slotError}</p> : null}

            {recommendedSlots ? (
              <div className="mt-3 flex flex-wrap gap-2">
                {recommendedSlots.map((slot, index) => (
                  <button
                    key={index}
                    type="button"
                    onClick={() => handleSelectSlot(slot)}
                    className="rounded-full bg-blue-100 px-3 py-1.5 text-xs font-medium text-blue-700 transition hover:bg-blue-200 dark:bg-blue-900/40 dark:text-blue-300 dark:hover:bg-blue-900/60"
                    title={slot.reason}
                  >
                    {formatTimeLabel(parseMinutes(extractTime(slot.start_time)))} - {formatTimeLabel(parseMinutes(extractTime(slot.end_time)))}
                  </button>
                ))}
                {recommendedSlots.length === 0 ? (
                  <span className="text-xs text-slate-500">No recommended slots found for this date.</span>
                ) : null}
              </div>
            ) : null}
          </div>

          <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium text-slate-800 dark:text-slate-200">Meeting block</p>
                <p className="text-xs text-slate-500 dark:text-slate-400">Drag this into the grid to place it.</p>
              </div>
              <button
                type="button"
                draggable
                onDragStart={() => setDraggingBlock(true)}
                onDragEnd={() => setDraggingBlock(false)}
                className="cursor-grab rounded-xl bg-blue-600 px-4 py-3 text-left text-white shadow-sm transition hover:bg-blue-500 active:cursor-grabbing"
              >
                <div className="text-xs font-semibold uppercase tracking-wide opacity-80">Draft meeting</div>
                <div className="mt-1 text-sm font-semibold">{title.trim() || "Untitled"}</div>
                <div className="mt-1 text-xs opacity-90">{formatTimeLabel(parseMinutes(startTime))} - {formatTimeLabel(parseMinutes(endTime))}</div>
              </button>
            </div>
            <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
              You can also click a slot in the calendar below to snap the block into place.
            </p>
          </div>
        </div>

        <div className="space-y-4">
          <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 dark:border-slate-700 dark:bg-slate-950/40">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium text-slate-700 dark:text-slate-300">Weekly calendar</p>
                <p className="text-xs text-slate-500 dark:text-slate-400">Drag the meeting block onto a slot.</p>
              </div>
              <div className="flex gap-2 text-[11px] text-slate-500 dark:text-slate-400">
                <span className="rounded-full bg-emerald-500/15 px-2 py-1 text-emerald-700 dark:text-emerald-300">All selected available</span>
                <span className="rounded-full bg-amber-500/15 px-2 py-1 text-amber-700 dark:text-amber-300">Partial</span>
                <span className="rounded-full bg-rose-500/15 px-2 py-1 text-rose-700 dark:text-rose-300">Unavailable</span>
              </div>
            </div>

            <div className="mt-4 overflow-x-auto">
              <div className="min-w-[760px]">
                <div className="grid grid-cols-[72px_repeat(7,minmax(0,1fr))] border-b border-slate-200 dark:border-slate-800">
                  <div />
                  {weekDays.map((day) => (
                    <div key={day.index} className="px-2 py-3 text-center text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                      {day.label}
                    </div>
                  ))}
                </div>

                {HOURS.map((hour) => (
                  <div key={hour} className="grid grid-cols-[72px_repeat(7,minmax(0,1fr))] border-b border-slate-100 dark:border-slate-800">
                    <div className="px-2 py-3 text-right text-xs font-medium text-slate-400 dark:text-slate-500">
                      {hour === 12 ? "12pm" : hour < 12 ? `${hour}am` : `${hour - 12}pm`}
                    </div>

                    {weekDays.map((day) => {
                      const dayStart = hour * 60;
                      const dayEnd = dayStart + 60;
                      const overlap = getAvailabilityBlocks(selectedMembers, availabilitySlots, day.index, dayStart, dayEnd);
                      const previewMatches = dragPreview?.dayOfWeek === day.index && dragPreview.startMinutes < dayEnd && dragPreview.endMinutes > dayStart;
                      const placedMatches = placedMeeting?.dayOfWeek === day.index && placedMeeting.startMinutes < dayEnd && placedMeeting.endMinutes > dayStart;
                      const cellBackground = placedMatches
                        ? "bg-blue-50 dark:bg-blue-950/30"
                        : previewMatches
                          ? "bg-blue-50/70 dark:bg-blue-950/20"
                          : overlap.status === "full"
                            ? "bg-emerald-50/40 dark:bg-emerald-950/20"
                            : overlap.status === "partial"
                              ? "bg-amber-50/50 dark:bg-amber-950/20"
                              : "bg-rose-50/40 dark:bg-rose-950/20";

                      return (
                        <div
                          key={`${day.index}-${hour}`}
                          className={`relative min-h-[56px] border-l border-slate-100 p-1 dark:border-slate-800 ${cellBackground}`}
                          onDragOver={(event) => {
                            event.preventDefault();
                            if (!draggingBlock) return;
                            const rect = event.currentTarget.getBoundingClientRect();
                            const relativeY = event.clientY - rect.top;
                            updatePlacement(day.index, hour, relativeY, rect.height || 56);
                            setDragPreview({
                              date: formatDateInput(addDays(weekStart, day.index)),
                              dayOfWeek: day.index,
                              startMinutes: placedMeeting?.startMinutes ?? dayStart,
                              endMinutes: placedMeeting?.endMinutes ?? dayEnd,
                            });
                          }}
                          onDrop={(event) => {
                            event.preventDefault();
                            const rect = event.currentTarget.getBoundingClientRect();
                            const relativeY = event.clientY - rect.top;
                            updatePlacement(day.index, hour, relativeY, rect.height || 56);
                            setDraggingBlock(false);
                          }}
                          onClick={(event) => {
                            const rect = event.currentTarget.getBoundingClientRect();
                            const relativeY = event.clientY - rect.top;
                            updatePlacement(day.index, hour, relativeY, rect.height || 56);
                            setDragPreview(null);
                          }}
                          onMouseLeave={() => {
                            if (!draggingBlock) {
                              setDragPreview(null);
                            }
                          }}
                        >
                          {selectedMembers.map((member, memberIndex) => {
                            const memberSlots = availabilitySlots.filter(
                              (slot) => slot.user_id === member.id && slot.day_of_week === day.index,
                            );
                            return memberSlots.map((slot) => {
                              const slotStart = parseMinutes(slot.start_time);
                              const slotEnd = parseMinutes(slot.end_time);
                              if (slotStart >= dayEnd || slotEnd <= dayStart) return null;

                              const top = timeChunkTop(slotStart, hour);
                              const height = timeChunkHeight(slotStart, slotEnd, hour);
                              if (height <= 0) return null;

                              const palette = ["#2563eb", "#0f766e", "#7c3aed", "#ea580c", "#16a34a", "#db2777"];
                              const color = palette[memberIndex % palette.length];

                              return (
                                <div
                                  key={`${member.id}-${slot.id}-${day.index}-${hour}`}
                                  className="absolute left-1 right-1 rounded-md px-2 py-1 text-[10px] font-medium text-white shadow-sm"
                                  style={{
                                    top: `${top}%`,
                                    height: `${Math.max(height, 20)}%`,
                                    backgroundColor: color,
                                    opacity: 0.82,
                                  }}
                                  title={`${memberDisplayName(member)}: ${slot.start_time.slice(0, 5)} - ${slot.end_time.slice(0, 5)}`}
                                >
                                  {member.first_name.charAt(0)}{member.last_name.charAt(0)}
                                </div>
                              );
                            });
                          })}

                          {placedMatches && placedMeeting ? (
                            <div
                              className="absolute left-1 right-1 rounded-lg bg-blue-600 px-2 py-1 text-[11px] font-semibold text-white shadow-md"
                              style={{
                                top: `${timeChunkTop(placedMeeting.startMinutes, hour)}%`,
                                height: `${Math.max(timeChunkHeight(placedMeeting.startMinutes, placedMeeting.endMinutes, hour), 18)}%`,
                              }}
                            >
                              <div className="truncate">{title.trim() || "Meeting block"}</div>
                              <div className="text-[10px] opacity-85">{formatTimeLabel(placedMeeting.startMinutes)} - {formatTimeLabel(placedMeeting.endMinutes)}</div>
                            </div>
                          ) : null}

                          {previewMatches && dragPreview && !placedMatches ? (
                            <div
                              className="absolute left-1 right-1 rounded-lg border border-dashed border-blue-500/80 bg-blue-500/10 px-2 py-1 text-[11px] font-medium text-blue-700 dark:text-blue-300"
                              style={{
                                top: `${timeChunkTop(dragPreview.startMinutes, hour)}%`,
                                height: `${Math.max(timeChunkHeight(dragPreview.startMinutes, dragPreview.endMinutes, hour), 18)}%`,
                              }}
                            >
                              {title.trim() || "Meeting block"}
                            </div>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                ))}
              </div>
            </div>
          </div>

          {placedMeeting ? (
            <div className="rounded-xl border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-900 dark:border-blue-900/40 dark:bg-blue-950/30 dark:text-blue-200">
              Placed for {weekDays[placedMeeting.dayOfWeek]?.label} from {formatTimeLabel(placedMeeting.startMinutes)} to {formatTimeLabel(placedMeeting.endMinutes)}.
            </div>
          ) : null}

          {error ? <p className="text-sm text-rose-500">{error}</p> : null}
          {success ? <p className="text-sm text-emerald-600 dark:text-emerald-400">{success}</p> : null}

          <div className="flex justify-end gap-3 pt-1">
            <button
              type="submit"
              disabled={saving}
              className="rounded-lg bg-blue-600 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-blue-500 disabled:opacity-50"
            >
              {saving ? "Creating..." : "Create Meeting"}
            </button>
          </div>
        </div>
      </form>
    </section>
  );
}
