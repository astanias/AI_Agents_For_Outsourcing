document.addEventListener("DOMContentLoaded", () => {
  initAvailabilityCalendar();
});

function initAvailabilityCalendar() {
  const roots = Array.from(document.querySelectorAll("[data-availability-calendar-root]"));
  if (!roots.length) {
    return;
  }

  function cellKey(dayOfWeek, startMinutes) {
    return `${dayOfWeek}:${startMinutes}`;
  }

  function parseInitialCells(rawValue) {
    if (!rawValue) {
      return [];
    }

    try {
      const parsed = JSON.parse(rawValue);
      if (!Array.isArray(parsed)) {
        return [];
      }
      return parsed
        .map((item) => ({
          day_of_week: Number(item.day_of_week),
          start_minutes: Number(item.start_minutes),
        }))
        .filter((item) => Number.isFinite(item.day_of_week) && Number.isFinite(item.start_minutes));
    } catch {
      return [];
    }
  }

  function serializeCells(cellSet) {
    return JSON.stringify(
      Array.from(cellSet)
        .map((key) => {
          const [dayOfWeek, startMinutes] = key.split(":").map(Number);
          return { day_of_week: dayOfWeek, start_minutes: startMinutes };
        })
        .sort((left, right) => (left.day_of_week - right.day_of_week) || (left.start_minutes - right.start_minutes)),
    );
  }

  function setsEqual(leftSet, rightSet) {
    if (leftSet.size !== rightSet.size) {
      return false;
    }
    for (const value of leftSet) {
      if (!rightSet.has(value)) {
        return false;
      }
    }
    return true;
  }

  function updateSummary(root, cellCount) {
    const summary = root.querySelector("[data-availability-summary]");
    if (!summary) {
      return;
    }
    summary.textContent = `${cellCount} cell${cellCount === 1 ? "" : "s"} selected`;
  }

  function updateHiddenInput(root, cellSet) {
    const hiddenInput = root.querySelector("[data-availability-selected-cells-input]");
    if (!hiddenInput) {
      return;
    }
    hiddenInput.value = serializeCells(cellSet);
  }

  function refreshGrid(root, cellSet) {
    const selector = "[data-availability-cell], [data-group-meeting-slot], .group-meeting-slot";
    const cells = Array.from(root.querySelectorAll(selector));
    cells.forEach((cell) => {
      const dayOfWeek = Number(cell.dataset.dayOfWeek || cell.getAttribute('data-day-of-week') || "0");
      const startMinutes = Number(cell.dataset.startMinutes || cell.getAttribute('data-start-minutes') || "0");
      const key = cellKey(dayOfWeek, startMinutes);
      const isSelected = cellSet.has(key);

      // support group-meeting-slot styling
      if (cell.classList.contains('group-meeting-slot')) {
        cell.classList.toggle('group-meeting-slot-full', isSelected);
        cell.classList.toggle('group-meeting-slot-none', !isSelected);
      }

      // support original availability button styling
      if (cell.hasAttribute('data-availability-cell')) {
        cell.classList.toggle('is-selected', isSelected);
      }
    });
    updateSummary(root, cellSet.size);
    updateHiddenInput(root, cellSet);
  }

  function getCellFromPoint(root, clientX, clientY) {
    const element = document.elementFromPoint(clientX, clientY);
    if (!(element instanceof HTMLElement)) {
      return null;
    }
    return element.closest("[data-availability-cell], [data-group-meeting-slot], .group-meeting-slot");
  }

  function applyRectangle(cellSet, startCell, endCell, mode) {
    const startDay = Math.min(startCell.dayOfWeek, endCell.dayOfWeek);
    const endDay = Math.max(startCell.dayOfWeek, endCell.dayOfWeek);
    const startMinute = Math.min(startCell.startMinutes, endCell.startMinutes);
    const endMinute = Math.max(startCell.startMinutes, endCell.startMinutes) + 15;

    for (let dayOfWeek = startDay; dayOfWeek <= endDay; dayOfWeek += 1) {
      for (let minuteValue = startMinute; minuteValue < endMinute; minuteValue += 15) {
        const key = cellKey(dayOfWeek, minuteValue);
        if (mode === "erase") {
          cellSet.delete(key);
        } else {
          cellSet.add(key);
        }
      }
    }
  }

  roots.forEach((root) => {
    const grid = root.querySelector("[data-availability-grid]") || root.querySelector("[data-group-meeting-grid]") || root.querySelector('.group-meeting-grid');
    const clearButton = root.querySelector("[data-availability-clear]");
    const saveButton = root.querySelector("[data-availability-save]");
    if (!grid || !clearButton || !saveButton) {
      return;
    }

    const initialCells = parseInitialCells(root.dataset.availabilitySelectedCells || "");
    let cellSet = new Set(initialCells.map((cell) => cellKey(cell.day_of_week, cell.start_minutes)));
    let dragging = false;
    let dragMode = "add";
    let startCell = null;
    let activePointerId = null;
    let baseCells = new Set(cellSet);
    let previewCells = new Set(cellSet);
    let savedCellSet = new Set(cellSet);

    function updateSaveButtonState() {
      const hasChanges = !setsEqual(cellSet, savedCellSet);
      saveButton.disabled = !hasChanges;
      saveButton.textContent = hasChanges ? "Save" : "Saved";
    }

    // ensure pointer events work smoothly on touch devices
    try {
      grid.style.touchAction = 'none';
    } catch (e) {}

    refreshGrid(root, cellSet);
    updateSaveButtonState();

    function commitPreview(cell) {
      if (!startCell || !cell) {
        return;
      }
      const mode = dragMode;
      applyRectangle(cellSet, startCell, cell, mode);
      refreshGrid(root, cellSet);
    }

    function getClosestCell(el) {
      if (!(el instanceof HTMLElement)) return null;
      return el.closest('[data-availability-cell], [data-group-meeting-slot], .group-meeting-slot');
    }

    grid.addEventListener("pointerdown", (event) => {
      const targetCell = getClosestCell(event.target instanceof HTMLElement ? event.target : null);
      if (!(targetCell instanceof HTMLElement)) {
        return;
      }

      event.preventDefault();
      activePointerId = event.pointerId;
      dragging = true;
      baseCells = new Set(cellSet);
      previewCells = new Set(baseCells);
      startCell = {
        dayOfWeek: Number(targetCell.dataset.dayOfWeek || targetCell.getAttribute('data-day-of-week') || "0"),
        startMinutes: Number(targetCell.dataset.startMinutes || targetCell.getAttribute('data-start-minutes') || "0"),
      };
      dragMode = cellSet.has(cellKey(startCell.dayOfWeek, startCell.startMinutes)) ? "erase" : "add";
      applyRectangle(previewCells, startCell, startCell, dragMode);
      refreshGrid(root, previewCells);
      grid.setPointerCapture(event.pointerId);
    });

    grid.addEventListener("pointermove", (event) => {
      if (!dragging || event.pointerId !== activePointerId || !startCell) {
        return;
      }
      const targetCell = getCellFromPoint(root, event.clientX, event.clientY) || getClosestCell(document.elementFromPoint(event.clientX, event.clientY));
      if (!targetCell) {
        return;
      }
      const currentCell = {
        dayOfWeek: Number(targetCell.dataset.dayOfWeek || targetCell.getAttribute('data-day-of-week') || "0"),
        startMinutes: Number(targetCell.dataset.startMinutes || targetCell.getAttribute('data-start-minutes') || "0"),
      };
      previewCells = new Set(baseCells);
      applyRectangle(previewCells, startCell, currentCell, dragMode);
      refreshGrid(root, previewCells);
    });

    function endDrag(event) {
      if (!dragging || event.pointerId !== activePointerId) {
        return;
      }
      dragging = false;
      cellSet = new Set(previewCells);
      startCell = null;
      activePointerId = null;
      grid.releasePointerCapture(event.pointerId);
      refreshGrid(root, cellSet);
      updateSaveButtonState();
    }

    grid.addEventListener("pointerup", endDrag);
    grid.addEventListener("pointercancel", endDrag);
    grid.addEventListener("lostpointercapture", () => {
      dragging = false;
      startCell = null;
      activePointerId = null;
      refreshGrid(root, cellSet);
      updateSaveButtonState();
    });

    clearButton.addEventListener("click", () => {
      cellSet.clear();
      refreshGrid(root, cellSet);
      updateSaveButtonState();
    });

    const form = root.tagName === 'FORM' ? root : root.querySelector("form");
    form?.addEventListener("submit", (event) => {
      if (setsEqual(cellSet, savedCellSet)) {
        event.preventDefault();
        updateSaveButtonState();
        return;
      }

      updateHiddenInput(root, cellSet);
      saveButton.disabled = true;
      saveButton.textContent = "Saving...";
    });
  });
}
