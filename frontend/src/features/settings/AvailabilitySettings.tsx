import React, { useEffect, useMemo, useRef, useState } from 'react';

import {
  fetchAvailability as fetchCalendarAvailability,
  createAvailability,
  deleteAvailability,
} from '../../services/calendarApi';

interface TimeSlot {
  day_of_week: number;
  start_time: string;
  end_time: string;
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const START_HOUR = 7;
const END_HOUR = 22;
const STEP_MINUTES = 15;
const CELLS_PER_DAY = ((END_HOUR - START_HOUR) * 60) / STEP_MINUTES;

function minutesToTime(minutes: number) {
  const hour = Math.floor(minutes / 60);
  const minute = minutes % 60;
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00`;
}

function timeToMinutes(time: string) {
  const [hour, minute] = time.slice(0, 5).split(':').map(Number);
  return hour * 60 + minute;
}

function formatLabel(minutes: number) {
  const hour = Math.floor(minutes / 60);
  const minute = minutes % 60;
  const ampm = hour < 12 ? 'AM' : 'PM';
  const displayHour = hour % 12 === 0 ? 12 : hour % 12;
  return minute === 0 ? `${displayHour}${ampm.toLowerCase()}` : `${displayHour}:${String(minute).padStart(2, '0')} ${ampm}`;
}

function cellKey(dayIndex: number, slotIndex: number) {
  return `${dayIndex}:${slotIndex}`;
}

function slotsToSelectionMap(slots: TimeSlot[]) {
  const selection = new Set<string>();

  for (const slot of slots) {
    const start = timeToMinutes(slot.start_time);
    const end = timeToMinutes(slot.end_time);
    for (let minute = start; minute < end; minute += STEP_MINUTES) {
      const slotIndex = Math.floor((minute - START_HOUR * 60) / STEP_MINUTES);
      if (slotIndex >= 0 && slotIndex < CELLS_PER_DAY) {
        selection.add(cellKey(slot.day_of_week, slotIndex));
      }
    }
  }

  return selection;
}

function selectionMapToSlots(selection: Set<string>) {
  const results: TimeSlot[] = [];

  for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
    let rangeStart: number | null = null;

    for (let slotIndex = 0; slotIndex < CELLS_PER_DAY; slotIndex += 1) {
      const isSelected = selection.has(cellKey(dayIndex, slotIndex));
      const isLastCell = slotIndex === CELLS_PER_DAY - 1;

      if (isSelected && rangeStart === null) {
        rangeStart = slotIndex;
      }

      if (rangeStart !== null && (!isSelected || isLastCell)) {
        const endIndex = isSelected && isLastCell ? slotIndex + 1 : slotIndex;
        if (endIndex > rangeStart) {
          results.push({
            day_of_week: dayIndex,
            start_time: minutesToTime(START_HOUR * 60 + rangeStart * STEP_MINUTES),
            end_time: minutesToTime(START_HOUR * 60 + endIndex * STEP_MINUTES),
          });
        }
        rangeStart = null;
      }
    }
  }

  return results;
}

export default function AvailabilitySettings() {
  const [selection, setSelection] = useState<Set<string>>(new Set());
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [existingSlots, setExistingSlots] = useState<any[]>([]);
  const [dragMode, setDragMode] = useState<'select' | 'erase' | null>(null);
  const isPointerDown = useRef(false);

  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => i), []);

  useEffect(() => {
    const fetchAvailability = async () => {
      try {
        const data = await fetchCalendarAvailability();
        setExistingSlots(data || []);
        setSelection(slotsToSelectionMap(data || []));
      } catch (error) {
        console.error('Failed to fetch availability', error);
        setFeedback('Unable to load weekly availability.');
      } finally {
        setIsLoading(false);
      }
    };
    fetchAvailability();
  }, []);

  useEffect(() => {
    const handleMouseUp = () => {
      isPointerDown.current = false;
      setDragMode(null);
    };

    window.addEventListener('mouseup', handleMouseUp);
    return () => window.removeEventListener('mouseup', handleMouseUp);
  }, []);

  function updateCell(dayIndex: number, slotIndex: number, nextValue: boolean) {
    const key = cellKey(dayIndex, slotIndex);
    setSelection((prev) => {
      const next = new Set(prev);
      if (nextValue) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  function handleCellMouseDown(dayIndex: number, slotIndex: number) {
    const key = cellKey(dayIndex, slotIndex);
    const currentlySelected = selection.has(key);
    isPointerDown.current = true;
    setDragMode(currentlySelected ? 'erase' : 'select');
    updateCell(dayIndex, slotIndex, !currentlySelected);
  }

  function handleCellMouseEnter(dayIndex: number, slotIndex: number) {
    if (!isPointerDown.current || !dragMode) return;
    updateCell(dayIndex, slotIndex, dragMode === 'select');
  }

  const handleSave = async () => {
    setIsSaving(true);
    setFeedback('');

    try {
      const payload = selectionMapToSlots(selection);

      if (existingSlots.length) {
        await Promise.all(existingSlots.map((s) => deleteAvailability(s.id)));
      }

      await Promise.all(
        payload.map((slot) =>
          createAvailability({
            day_of_week: slot.day_of_week,
            start_time: slot.start_time,
            end_time: slot.end_time,
          })
        )
      );

      const refreshed = await fetchCalendarAvailability();
      setExistingSlots(refreshed || []);
      setFeedback('Working hours saved successfully.');
    } catch (error) {
      console.error(error);
      setFeedback(error instanceof Error ? error.message : 'Failed to save schedule.');
    } finally {
      setIsSaving(false);
    }
  };

  const selectedCount = selection.size;

  if (isLoading) return <div className="p-8 text-slate-400">Loading schedule...</div>;

  return (
    <section className="mt-6 rounded-[28px] border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold text-slate-800 dark:text-white">Weekly Availability</h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">Drag across the grid to paint the times you are available.</p>
        </div>
        <div className="rounded-full bg-slate-100 px-4 py-1.5 text-xs font-semibold text-slate-600 dark:bg-slate-800 dark:text-slate-300">{selectedCount} time blocks selected</div>
      </div>

      {feedback ? (
        <div className="rounded-2xl border border-[#5c433a] bg-[#e8d7b8] px-4 py-3 text-sm text-slate-700 dark:border-slate-700 dark:bg-[#e8d7b8] dark:text-slate-700">{feedback}</div>
      ) : null}

      <div className="overflow-auto rounded-[24px] border border-slate-200 dark:border-slate-800">
        <div className="min-w-[900px] bg-white dark:bg-slate-950">
          <div className="group-meeting-grid-head">
            <span>Time</span>
            {DAYS.map((day) => (
              <span key={day}>{day}</span>
            ))}
          </div>

          <div className="group-meeting-grid" data-availability-group-grid>
            {Array.from({ length: CELLS_PER_DAY }, (_, slotIndex) => {
              const isHour = slotIndex % 4 === 0;
              const label = isHour ? formatLabel(START_HOUR * 60 + slotIndex * STEP_MINUTES) : '';

              return (
                <React.Fragment key={slotIndex}>
                  <div className={`group-meeting-time-cell${isHour ? '' : ' is-quarter'}`}>{label}</div>

                  {days.map((dayIndex) => {
                    const selected = selection.has(cellKey(dayIndex, slotIndex));
                    const statusClass = selected ? 'group-meeting-slot-full' : 'group-meeting-slot-none';

                    return (
                      <div
                        key={`${dayIndex}-${slotIndex}`}
                        className={`group-meeting-slot ${statusClass}`}
                        data-availability-cell
                        data-day-of-week={dayIndex}
                        data-start-minutes={START_HOUR * 60 + slotIndex * STEP_MINUTES}
                        data-end-minutes={START_HOUR * 60 + (slotIndex + 1) * STEP_MINUTES}
                        onMouseDown={() => handleCellMouseDown(dayIndex, slotIndex)}
                        onMouseEnter={() => handleCellMouseEnter(dayIndex, slotIndex)}
                        role="button"
                        aria-label={`${DAYS[dayIndex]} ${formatLabel(START_HOUR * 60 + slotIndex * STEP_MINUTES)} ${selected ? 'selected' : 'unselected'}`}
                      />
                    );
                  })}
                </React.Fragment>
              );
            })}

            <div className="group-meeting-overlay" data-availability-overlay aria-hidden="true" />
          </div>

          <div className="group-meeting-summary">
            <p>
              Selected slots: <strong>{selectedCount} painted</strong>
            </p>
            <p className="field-help">Drag across the grid to paint your availability.</p>
          </div>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-sm text-slate-500 dark:text-slate-400">
        <div className="flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-[#50A3A4]" /> Available
          </span>
          <span className="inline-flex items-center gap-2">
            <span className="h-3 w-3 rounded-full bg-[#e8d7b8] ring-1 ring-black/20" /> Unavailable
          </span>
        </div>
        <button
          type="button"
          onClick={handleSave}
          disabled={isSaving}
          className="rounded-full bg-[#50A3A4] px-6 py-2.5 font-medium text-white transition hover:bg-[#439293] disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isSaving ? 'Saving...' : 'Save Changes'}
        </button>
      </div>
    </section>
  );
}
