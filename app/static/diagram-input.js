// A pasted or dropped image becomes the diagram file of the form (one diagram per workflow).
// On the create page ("allow_bpmn"), the "Start from" radios switch the form between "A description" and
// "A BPMN file" (a BPMN file input and a required Context). Dropping or pasting a .bpmn file selects
// "A BPMN file" and puts the file in the BPMN input. Both modes post to the same URL.
// Used on the create and edit pages (see partials/diagram_input.html).
(function () {
  var input = document.getElementById("diagram");
  var area = document.getElementById("description");
  var drop = document.getElementById("diagram-drop");
  if (!input || !area || !drop) return;
  var preview = document.getElementById("diagram-preview");
  var img = document.getElementById("diagram-img");
  var nameEl = document.getElementById("diagram-name");
  var current = document.getElementById("diagram-current");
  var removeFlag = document.getElementById("remove-diagram");
  var removedNote = document.getElementById("diagram-removed");
  var url = null;
  var form = area.form;
  var allowBpmn = drop.dataset.bpmn === "1";
  var radios = allowBpmn ? form.querySelectorAll('input[name="start"]') : [];
  var bpmnInput = document.getElementById("bpmn-file");
  var submitLabel = document.getElementById("submit-label");

  function isBpmn(file) { return allowBpmn && !!file && /\.bpmn$/i.test(file.name); }
  function mode() {
    for (var i = 0; i < radios.length; i++) if (radios[i].checked) return radios[i].value;
    return "describe";
  }
  // Show the blocks of the chosen mode, and set what the browser must enforce.
  function applyMode() {
    if (!allowBpmn) return;
    var bpmn = mode() === "bpmn";
    area.setCustomValidity("");
    form.querySelectorAll("[data-mode]").forEach(function (el) { el.hidden = el.dataset.mode !== (bpmn ? "bpmn" : "describe"); });
    if (bpmnInput) bpmnInput.required = bpmn;
    area.required = bpmn;
    if (bpmn) area.setAttribute("minlength", "40"); else area.removeAttribute("minlength");
    area.placeholder = bpmn ? area.dataset.placeholderBpmn : area.dataset.placeholderDescribe;
    if (submitLabel) submitLabel.textContent = bpmn ? submitLabel.dataset.bpmn : submitLabel.dataset.describe;
  }
  function setMode(value) {
    radios.forEach(function (r) { r.checked = r.value === value; });
    applyMode();
  }
  function useBpmn(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    bpmnInput.files = dt.files;
    setMode("bpmn");
  }

  function hasCurrent() { return current && removeFlag && !removeFlag.value; }
  function show() {
    var file = input.files[0];
    if (url) { URL.revokeObjectURL(url); url = null; }
    if (current) current.hidden = !!file || !hasCurrent();
    if (!file) { preview.hidden = true; return; }
    if (file.type.indexOf("image/") === 0) {
      url = URL.createObjectURL(file);
      img.src = url;
      img.hidden = false;
    } else {
      img.hidden = true;
    }
    nameEl.textContent = file.name + " · " + (file.size / 1e6).toFixed(2) + " MB"
      + (current ? " (replaces the current diagram)" : "");
    preview.hidden = false;
  }
  function use(file, name) {
    var dt = new DataTransfer();
    dt.items.add(name ? new File([file], name, { type: file.type }) : file);
    input.files = dt.files;
    if (allowBpmn && mode() === "bpmn") setMode("describe");  // a diagram means describe mode
    show();
  }
  area.addEventListener("paste", function (e) {
    var items = (e.clipboardData && e.clipboardData.items) || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind !== "file") continue;
      var pasted = items[i].getAsFile();
      if (isBpmn(pasted) && bpmnInput) {
        e.preventDefault();
        useBpmn(pasted);
        return;
      }
      if (items[i].type.indexOf("image/") === 0) {
        e.preventDefault();  // keep pasted text as usual; only an image is captured
        var ext = items[i].type.split("/")[1] || "png";
        use(pasted, "pasted-diagram." + ext);
        return;
      }
    }
  });
  ["dragover", "dragenter"].forEach(function (t) {
    [drop, area].forEach(function (el) {
      el.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add("is-over"); });
    });
  });
  ["dragleave", "drop"].forEach(function (t) {
    [drop, area].forEach(function (el) {
      el.addEventListener(t, function () { drop.classList.remove("is-over"); });
    });
  });
  [drop, area].forEach(function (el) {
    el.addEventListener("drop", function (e) {
      var file = e.dataTransfer && e.dataTransfer.files[0];
      if (isBpmn(file) && bpmnInput) {
        e.preventDefault();
        useBpmn(file);
      } else if (file && (file.type.indexOf("image/") === 0 || file.type === "application/pdf")) {
        e.preventDefault();
        use(file);
      }
    });
  });
  input.addEventListener("change", show);
  radios.forEach(function (r) { r.addEventListener("change", applyMode); });
  applyMode();
  document.getElementById("diagram-remove").addEventListener("click", function () {
    input.value = "";
    show();
  });
  if (current) {
    document.getElementById("diagram-current-remove").addEventListener("click", function () {
      removeFlag.value = "1";
      removedNote.hidden = false;
      show();
    });
    document.getElementById("diagram-undo").addEventListener("click", function (e) {
      e.preventDefault();
      removeFlag.value = "";
      removedNote.hidden = true;
      show();
    });
  }
  // Describe mode: text or a diagram, at least one of them. (BPMN mode: the browser enforces the
  // required context, and the server checks it again.)
  form.addEventListener("submit", function (e) {
    if (allowBpmn && mode() === "bpmn") return;
    if (!input.files.length && !hasCurrent() && area.value.trim().length < 40) {
      e.preventDefault();
      area.setCustomValidity("Describe the workflow in more detail, or add a diagram.");
      area.reportValidity();
    }
  });
  area.addEventListener("input", function () { area.setCustomValidity(""); });
})();
