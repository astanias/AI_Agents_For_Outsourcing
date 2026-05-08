function pad(value) {
  return String(value).padStart(2, "0");
}

function formatDateInput(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function addDays(date, days) {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function getWeekStart(date) {
  const result = new Date(date);
  result.setHours(0, 0, 0, 0);
  result.setDate(result.getDate() - result.getDay());
  return result;
}

function minutesToTime(minutes) {
  return `${pad(Math.floor(minutes / 60))}:${pad(minutes % 60)}`;
}

function formatDisplayTime(minutes) {
  const hour24 = Math.floor(minutes / 60);
  const minute = minutes % 60;
  const suffix = hour24 < 12 ? "AM" : "PM";
  const hour12 = hour24 % 12 === 0 ? 12 : hour24 % 12;
  return `${hour12}:${pad(minute)} ${suffix}`;
}

function parseTime(value) {
  const [hour, minute] = String(value || "00:00").slice(0, 5).split(":").map(Number);
  return hour * 60 + minute;
}

function parseDateTimeLocal(value) {
  if (!value) {
    const now = new Date();
    now.setMinutes(0, 0, 0);
    now.setHours(now.getHours() + 1);
    return now;
  }
  return new Date(value.replace(" ", "T"));
}

function minutesToDateTimeLocal(baseDate, minutes) {
  const date = new Date(baseDate);
  date.setHours(Math.floor(minutes / 60), minutes % 60, 0, 0);
  return `${formatDateInput(date)}T${minutesToTime(minutes)}:00`;
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function snapToStep(value, step) {
  return Math.round(value / step) * step;
}

function parseDateString(value) {
  const [year, month, day] = String(value || "").split("-").map(Number);
  if (!year || !month || !day) return null;
  return new Date(year, month - 1, day, 12, 0, 0, 0);
}

function getDurationLabel(durationMinutes) {
  return `${durationMinutes} min`;
}

function initPlanner(root) {
  const form = root.querySelector("[data-group-meeting-form]");
  const draft = root.querySelector("[data-group-meeting-draft]");
  const titleInput = root.querySelector("[data-group-meeting-title]");
  const durationInput = root.querySelector("[data-group-meeting-duration]");
  const durationLabel = root.querySelector("[data-group-meeting-duration-label]");
  const typeHidden = root.querySelector("[data-group-meeting-type]");
  const startHidden = root.querySelector("[data-group-meeting-start]");
  const endHidden = root.querySelector("[data-group-meeting-end]");
  const summary = root.querySelector("[data-group-meeting-summary]");
  const draftTitle = root.querySelector("[data-group-meeting-draft-title]");
  const draftTime = root.querySelector("[data-group-meeting-draft-time]");
  const grid = root.querySelector("[data-group-meeting-grid]");
  const overlay = root.querySelector("[data-group-meeting-overlay]");
  const meetingTypeRadios = Array.from(root.querySelectorAll('input[name="meeting_type_ui"]'));
  const slots = Array.from(root.querySelectorAll("[data-group-meeting-slot]"));

  if (
    !form ||
    !draft ||
    !titleInput ||
    !durationInput ||
    !durationLabel ||
    !typeHidden ||
    !startHidden ||
    !endHidden ||
    !summary ||
    !draftTitle ||
    !draftTime ||
    !grid ||
    !overlay
  ) {
    return;
  }

  const weekStart = getWeekStart(parseDateTimeLocal(startHidden.value));
  const slotStep = 15;
  const dayStart = 7 * 60;
  const dayEnd = 23 * 60;
  const holdDelayMs = 80;
  let dragging = false;
  let placement = null;
  let holdTimerId = null;
  let blockHoldActive = false;

  function setMeetingTypeFromUi() {
    const selected = meetingTypeRadios.find((radio) => radio.checked);
    typeHidden.value = selected?.value === "virtual" ? "virtual" : "in_person";
  }

  function clearPlacementRendering() {
    slots.forEach((slot) => {
      slot.classList.remove("is-active", "is-selected-start", "is-selected-middle", "is-selected-end");
    });
    overlay.replaceChildren();
  }

  function setSummary(dayOfWeek, startMinutes, endMinutes) {
    const actualDate = addDays(weekStart, dayOfWeek);
    const dayLabel = actualDate.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
    summary.textContent = `${dayLabel} • ${formatDisplayTime(startMinutes)} - ${formatDisplayTime(endMinutes)}`;
    draftTitle.textContent = titleInput.value.trim() || "Team sync";
    draftTime.textContent = `${formatDisplayTime(startMinutes)} - ${formatDisplayTime(endMinutes)}`;
  }

  function renderPlacement() {
    clearPlacementRendering();
    if (!placement) return;

    const activeSlots = [];
    const unavailableNameSet = new Set();

    slots.forEach((slot) => {
      const slotDay = Number(slot.dataset.dayOfWeek || "0");
      const slotStart = Number(slot.dataset.startMinutes || "0");
      const slotEnd = Number(slot.dataset.endMinutes || "0");
      const overlaps = placement.dayOfWeek === slotDay && placement.startMinutes < slotEnd && placement.endMinutes > slotStart;
      if (!overlaps) return;

      const isStart = slotStart === placement.startMinutes;
      const isEnd = slotEnd === placement.endMinutes;
      slot.classList.add("is-active");
      slot.classList.add(isStart ? "is-selected-start" : isEnd ? "is-selected-end" : "is-selected-middle");
      activeSlots.push(slot);

      if (slot.dataset.status === "partial") {
        const rawMembers = String(slot.dataset.unavailableMembers || "");
        if (rawMembers) {
          rawMembers
            .split("||")
            .map((name) => name.trim())
            .filter(Boolean)
            .forEach((name) => unavailableNameSet.add(name));
        }
      }
    });

    if (!activeSlots.length) {
      return;
    }

    activeSlots.sort((left, right) => Number(left.dataset.startMinutes || "0") - Number(right.dataset.startMinutes || "0"));
    const firstSlot = activeSlots[0];
    const lastSlot = activeSlots[activeSlots.length - 1];

    const gridRect = grid.getBoundingClientRect();
    const firstRect = firstSlot.getBoundingClientRect();
    const lastRect = lastSlot.getBoundingClientRect();

    const block = document.createElement("div");
    block.className = "group-meeting-block";
    block.style.left = `${firstRect.left - gridRect.left + 4}px`;
    block.style.top = `${firstRect.top - gridRect.top + 2}px`;
    block.style.width = `${Math.max(0, firstRect.width - 8)}px`;
    block.style.height = `${Math.max(0, lastRect.bottom - firstRect.top - 4)}px`;

    const blockCopy = document.createElement("div");
    blockCopy.className = "group-meeting-block-copy";
    blockCopy.innerHTML = `
      <strong>${draftTitle.textContent || "Team sync"}</strong>
      <span>${formatDisplayTime(placement.startMinutes)} - ${formatDisplayTime(placement.endMinutes)}</span>
    `;
    block.appendChild(blockCopy);
    attachBlockHoldHandlers(block);
    overlay.appendChild(block);

    const unavailableNames = Array.from(unavailableNameSet);
    if (unavailableNames.length > 0) {
      const shownNames = unavailableNames.slice(0, 3);
      const remaining = unavailableNames.length - shownNames.length;

      const popover = document.createElement("aside");
      popover.className = "group-meeting-conflict-popover";
      popover.innerHTML = `
        <p class="group-meeting-conflict-title">Cannot attend</p>
        <p class="group-meeting-conflict-names">${shownNames.join(", ")}</p>
        ${remaining > 0 ? `<p class="group-meeting-conflict-more">+${remaining} more unavailable</p>` : ""}
      `;
      overlay.appendChild(popover);
    }
  }

  function updateHiddenTimes(dayOfWeek, startMinutes, durationMinutes) {
    const actualDate = addDays(weekStart, dayOfWeek);
    const endMinutes = startMinutes + durationMinutes;
    startHidden.value = minutesToDateTimeLocal(actualDate, startMinutes);
    endHidden.value = minutesToDateTimeLocal(actualDate, endMinutes);
    placement = { dayOfWeek, startMinutes, endMinutes };
    setSummary(dayOfWeek, startMinutes, endMinutes);
    renderPlacement();
  }

  function placeFromSlot(slot) {
    const dayOfWeek = Number(slot.dataset.dayOfWeek || "0");
    const slotStart = Number(slot.dataset.startMinutes || "0");
    const duration = clamp(Number(durationInput.value || "60"), slotStep, dayEnd - slotStart);
    const startMinutes = clamp(snapToStep(slotStart, slotStep), dayStart, dayEnd - duration);
    updateHiddenTimes(dayOfWeek, startMinutes, duration);
  }

  function getSlotFromPoint(clientX, clientY) {
    const target = document.elementFromPoint(clientX, clientY);
    if (!(target instanceof HTMLElement)) {
      return null;
    }
    return target.closest("[data-group-meeting-slot]");
  }

  function clearHoldTimer() {
    if (holdTimerId !== null) {
      window.clearTimeout(holdTimerId);
      holdTimerId = null;
    }
  }

  function startHoldDrag() {
    blockHoldActive = true;
    overlay.classList.add("is-hold-dragging");
  }

  function stopHoldDrag() {
    clearHoldTimer();
    if (!blockHoldActive) {
      return;
    }
    blockHoldActive = false;
    overlay.classList.remove("is-hold-dragging");
  }

  function attachBlockHoldHandlers(block) {
    block.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) {
        return;
      }

      clearHoldTimer();
      holdTimerId = window.setTimeout(() => {
        startHoldDrag();
      }, holdDelayMs);

      block.setPointerCapture(event.pointerId);
    });

    block.addEventListener("pointermove", (event) => {
      if (!blockHoldActive) {
        return;
      }

      const slot = getSlotFromPoint(event.clientX, event.clientY);
      if (slot) {
        placeFromSlot(slot);
      }
    });

    block.addEventListener("pointerup", () => {
      stopHoldDrag();
    });

    block.addEventListener("pointercancel", () => {
      stopHoldDrag();
    });

    block.addEventListener("lostpointercapture", () => {
      stopHoldDrag();
    });
  }

  draft.addEventListener("dragstart", () => {
    dragging = true;
    draft.classList.add("is-dragging");
  });

  draft.addEventListener("dragend", () => {
    dragging = false;
    draft.classList.remove("is-dragging");
  });

  slots.forEach((slot) => {
    slot.addEventListener("dragover", (event) => {
      if (!dragging) return;
      event.preventDefault();
      placeFromSlot(slot);
    });

    slot.addEventListener("drop", (event) => {
      event.preventDefault();
      dragging = false;
      draft.classList.remove("is-dragging");
      placeFromSlot(slot);
    });

    slot.addEventListener("click", () => {
      placeFromSlot(slot);
    });
  });

  titleInput.addEventListener("input", () => {
    draftTitle.textContent = titleInput.value.trim() || "Team sync";
    if (placement) {
      renderPlacement();
    }
  });

  function syncDuration() {
    const value = Number(durationInput.value || "60");
    durationLabel.textContent = getDurationLabel(value);
    if (placement) {
      updateHiddenTimes(placement.dayOfWeek, placement.startMinutes, value);
    }
  }

  durationInput.addEventListener("input", syncDuration);
  durationInput.addEventListener("change", syncDuration);

  meetingTypeRadios.forEach((radio) => {
    radio.addEventListener("change", setMeetingTypeFromUi);
  });

  form.addEventListener("submit", () => {
    setMeetingTypeFromUi();
    if (!placement) {
      const fallbackDate = parseDateTimeLocal(startHidden.value);
      updateHiddenTimes(fallbackDate.getDay(), parseTime("09:00"), Number(durationInput.value || "60"));
    }
  });

  window.addEventListener("resize", () => {
    if (placement) {
      renderPlacement();
    }
  });

  const initialDate = parseDateString(startHidden.value.slice(0, 10)) || new Date();
  const initialDay = initialDate.getDay();
  const initialStart = parseTime(startHidden.value.slice(11, 16) || "09:00");
  const initialEnd = parseTime(endHidden.value.slice(11, 16) || "10:00");

  placement = {
    dayOfWeek: initialDay,
    startMinutes: initialStart,
    endMinutes: initialEnd,
  };

  durationLabel.textContent = getDurationLabel(Number(durationInput.value || "60"));
  setMeetingTypeFromUi();
  setSummary(initialDay, initialStart, initialEnd);
  renderPlacement();
}

document.addEventListener("DOMContentLoaded", () => {
  Array.from(document.querySelectorAll("[data-group-meeting-planner-root]")).forEach(initPlanner);
});
