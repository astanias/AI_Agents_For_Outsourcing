(function () {
  var BELL_REFRESH_INTERVAL_MS = 10000;

  function dismissToast(toast) {
    if (!toast || toast.dataset.toastDismissed === "true") {
      return;
    }

    toast.dataset.toastDismissed = "true";
    toast.classList.add("toast-hiding");

    window.setTimeout(function () {
      if (toast.parentElement) {
        toast.parentElement.removeChild(toast);
      }
    }, 220);
  }

  function setupToasts() {
    document.querySelectorAll(".toast").forEach(function (toast) {
      if (toast.dataset.toastEnhanced === "true") {
        return;
      }

      toast.dataset.toastEnhanced = "true";

      var closeButton = document.createElement("button");
      closeButton.type = "button";
      closeButton.className = "toast-close";
      closeButton.setAttribute("aria-label", "Dismiss notification");
      closeButton.innerHTML = "&times;";
      closeButton.addEventListener("click", function () {
        dismissToast(toast);
      });

      toast.appendChild(closeButton);

      window.setTimeout(function () {
        dismissToast(toast);
      }, 3000);
    });
  }

  function syncAvatarPreview() {
    var preview = document.querySelector("[data-avatar-preview]");
    if (!preview) {
      return;
    }

    var selected = document.querySelector("[data-avatar-color-input]:checked");
    if (!selected) {
      return;
    }

    var nextColor = selected.getAttribute("data-avatar-color-value");
    if (nextColor) {
      preview.style.background = nextColor;
    }
  }

  function requestJson(url, options) {
    var requestOptions = options || {};
    var headers = new Headers(requestOptions.headers || {});
    headers.set("Accept", "application/json");

    if (requestOptions.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }

    return window.fetch(url, {
      method: requestOptions.method || "GET",
      headers: headers,
      body: requestOptions.body,
      credentials: "same-origin"
    }).then(function (response) {
      var contentType = response.headers.get("content-type") || "";
      var payloadPromise = contentType.indexOf("application/json") >= 0 ? response.json() : Promise.resolve(null);
      return payloadPromise.then(function (payload) {
        if (!response.ok) {
          var error = new Error((payload && payload.detail) || "Request failed");
          error.response = response;
          error.payload = payload;
          throw error;
        }
        return payload;
      });
    });
  }

  function formatBellTimestamp(value) {
    if (!value) {
      return "";
    }

    var parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return "";
    }

    return parsed.toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit"
    });
  }

  function formatRsvpStatus(value) {
    if (!value) {
      return "";
    }

    return value.charAt(0).toUpperCase() + value.slice(1);
  }

  function updateBellBadge(button, badge, unreadCount) {
    if (!button || !badge) {
      return;
    }

    if (!unreadCount) {
      badge.hidden = true;
      badge.textContent = "0";
      button.setAttribute("aria-label", "Open recent notifications");
      return;
    }

    badge.hidden = false;
    badge.textContent = String(unreadCount);
    button.setAttribute("aria-label", "Open recent notifications, " + unreadCount + " unread");
  }

  function setBellOpen(button, panel, isOpen) {
    if (!button || !panel) {
      return;
    }

    button.setAttribute("aria-expanded", isOpen ? "true" : "false");
    panel.hidden = !isOpen;
  }

  function createBellActionButton(config) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = config.className || "btn btn-soft btn-inline";
    button.textContent = config.label;
    button.addEventListener("click", config.onClick);
    return button;
  }

  function renderBellItem(item, state) {
    var article = document.createElement("article");
    article.className = "app-header-bell-item";
    if (item.is_unread) {
      article.classList.add("is-unread");
    }

    var head = document.createElement("div");
    head.className = "app-header-bell-item-head";

    var title = document.createElement("h4");
    title.className = "app-header-bell-item-title";
    title.textContent = item.title;

    var meta = document.createElement("div");
    meta.className = "app-header-bell-item-meta";

    var typePill = document.createElement("span");
    typePill.className = "app-header-bell-type";
    typePill.textContent = item.type;
    meta.appendChild(typePill);

    var time = document.createElement("span");
    time.className = "app-header-bell-item-time";
    time.textContent = formatBellTimestamp(item.created_at);
    meta.appendChild(time);

    head.appendChild(title);
    head.appendChild(meta);
    article.appendChild(head);

    var message = document.createElement("p");
    message.className = "app-header-bell-item-message";
    message.textContent = item.message;
    article.appendChild(message);

    if (item.current_status) {
      var status = document.createElement("p");
      status.className = "app-header-bell-item-status";
      status.textContent = "Current RSVP: " + formatRsvpStatus(item.current_status);
      article.appendChild(status);
    }

    var actions = document.createElement("div");
    actions.className = "app-header-bell-item-actions";

    if (item.can_rsvp && item.meeting_id) {
      [
        { status: "accepted", label: "Accept" },
        { status: "maybe", label: "Maybe" },
        { status: "declined", label: "Decline" }
      ].forEach(function (action) {
        actions.appendChild(
          createBellActionButton({
            className: "btn btn-soft btn-inline app-header-bell-rsvp-button",
            label: action.label,
            onClick: function () {
              state.submitRsvp(item, action.status);
            }
          })
        );
      });
    }

    if (item.open_url) {
      var link = document.createElement("a");
      link.className = "btn btn-primary btn-inline app-header-bell-open-link";
      link.href = item.open_url;
      link.textContent = "Open meeting";
      actions.appendChild(link);
    }

    if (actions.children.length) {
      article.appendChild(actions);
    }

    return article;
  }

  function setupNotificationBell() {
    var root = document.querySelector("[data-notification-bell]");
    if (!root) {
      return;
    }

    var button = root.querySelector("[data-notification-bell-button]");
    var badge = root.querySelector("[data-notification-bell-badge]");
    var panel = root.querySelector("[data-notification-bell-panel]");
    var summary = root.querySelector("[data-notification-bell-summary]");
    var list = root.querySelector("[data-notification-bell-list]");
    var empty = root.querySelector("[data-notification-bell-empty]");
    var markAll = root.querySelector("[data-notification-bell-mark-all]");

    if (!button || !badge || !panel || !summary || !list || !empty || !markAll) {
      return;
    }

    var state = {
      isOpen: false,
      isLoading: false,
      refreshTimer: null,
      render: function (payload) {
        payload = payload || { unread_count: 0, items: [] };
        updateBellBadge(button, badge, payload.unread_count || 0);
        summary.textContent = payload.items.length
          ? ((payload.unread_count || 0) + " unread from the last 24 hours.")
          : "No recent notifications from the last 24 hours.";
        markAll.disabled = !payload.items.length || !(payload.unread_count || 0);

        list.innerHTML = "";
        if (!payload.items.length) {
          empty.hidden = false;
          return;
        }

        empty.hidden = true;
        payload.items.forEach(function (item) {
          list.appendChild(renderBellItem(item, state));
        });
      },
      loadSummary: function () {
        return requestJson("/notifications/bell").then(function (payload) {
          state.render(payload);
          return payload;
        }).catch(function () {
          summary.textContent = "Notifications are unavailable right now.";
        });
      },
      openBell: function () {
        state.isOpen = true;
        setBellOpen(button, panel, true);
        state.isLoading = true;
        requestJson("/notifications/bell/open", { method: "POST" }).then(function (payload) {
          state.render(payload);
        }).catch(function () {
          summary.textContent = "Notifications are unavailable right now.";
        }).finally(function () {
          state.isLoading = false;
        });
      },
      closeBell: function () {
        state.isOpen = false;
        setBellOpen(button, panel, false);
      },
      submitRsvp: function (item, nextStatus) {
        if (!item || !item.meeting_id || state.isLoading) {
          return;
        }

        state.isLoading = true;
        requestJson("/api/meetings/" + item.meeting_id + "/rsvp", {
          method: "POST",
          body: JSON.stringify({ status: nextStatus })
        }).then(function () {
          return requestJson("/notifications/" + item.id + "/read", { method: "POST" }).catch(function () {
            return null;
          });
        }).then(function () {
          return state.loadSummary();
        }).finally(function () {
          state.isLoading = false;
        });
      }
    };

    button.addEventListener("click", function () {
      if (state.isOpen) {
        state.closeBell();
        return;
      }
      state.openBell();
    });

    markAll.addEventListener("click", function () {
      if (state.isLoading) {
        return;
      }

      state.isLoading = true;
      requestJson("/notifications/read-all", { method: "POST" }).then(function (payload) {
        state.render(payload);
      }).finally(function () {
        state.isLoading = false;
      });
    });

    document.addEventListener("click", function (event) {
      if (!state.isOpen) {
        return;
      }
      if (root.contains(event.target)) {
        return;
      }
      state.closeBell();
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && state.isOpen) {
        state.closeBell();
      }
    });

    state.loadSummary();
    state.refreshTimer = window.setInterval(function () {
      state.loadSummary();
    }, BELL_REFRESH_INTERVAL_MS);
  }

  document.addEventListener("click", function (event) {
    var closeButton = event.target.closest(".toast-close");
    if (!closeButton) {
      return;
    }

    event.preventDefault();
    dismissToast(closeButton.closest(".toast"));
  });

  document.addEventListener("change", function (event) {
    if (!event.target.matches("[data-avatar-color-input]")) {
      return;
    }

    syncAvatarPreview();
  });

  function initialize() {
    setupToasts();
    syncAvatarPreview();
    setupNotificationBell();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize);
  } else {
    initialize();
  }
})();
