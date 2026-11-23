// A pasted or dropped image becomes the diagram file of the form (one diagram per workflow).
// On the describe page a .bpmn file is imported instead: the form posts to /maestro/import-bpmn.
// Used on the describe and edit pages (see partials/diagram_input.html).
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
  var describeAction = form.getAttribute("action");
  var nameField = form.querySelector('input[name="name"]');
  var nameRequired = nameField ? nameField.required : false;

  function isBpmn(file) { return allowBpmn && !!file && /\.bpmn$/i.test(file.name); }

  function hasCurrent() { return current && removeFlag && !removeFlag.value; }
  function show() {
    var file = input.files[0];
    if (url) { URL.revokeObjectURL(url); url = null; }
    if (current) current.hidden = !!file || !hasCurrent();
    var bpmn = isBpmn(file);
    form.setAttribute("action", bpmn ? "/maestro/import-bpmn" : describeAction);
    if (nameField) nameField.required = bpmn ? false : nameRequired;  // the process name is used
    if (!file) { preview.hidden = true; return; }
    if (!bpmn && file.type.indexOf("image/") === 0) {
      url = URL.createObjectURL(file);
      img.src = url;
      img.hidden = false;
    } else {
      img.hidden = true;
    }
    nameEl.textContent = file.name + " · " + (file.size / 1e6).toFixed(2) + " MB"
      + (bpmn ? " (BPMN import: the process name is used)" : current ? " (replaces the current diagram)" : "");
    preview.hidden = false;
  }
  function use(file, name) {
    var dt = new DataTransfer();
    dt.items.add(name ? new File([file], name, { type: file.type }) : file);
    input.files = dt.files;
    show();
  }
  area.addEventListener("paste", function (e) {
    var items = (e.clipboardData && e.clipboardData.items) || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].kind === "file" && items[i].type.indexOf("image/") === 0) {
        e.preventDefault();  // keep pasted text as usual; only an image is captured
        var ext = items[i].type.split("/")[1] || "png";
        use(items[i].getAsFile(), "pasted-diagram." + ext);
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
      if (file && (file.type.indexOf("image/") === 0 || file.type === "application/pdf" || isBpmn(file))) {
        e.preventDefault();
        use(file);
      }
    });
  });
  input.addEventListener("change", show);
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
  // Text or a diagram: at least one of them.
  form.addEventListener("submit", function (e) {
    if (isBpmn(input.files[0])) return;  // a BPMN file replaces the description
    if (!input.files.length && !hasCurrent() && area.value.trim().length < 40) {
      e.preventDefault();
      area.setCustomValidity("Describe the workflow in more detail, or add a diagram.");
      area.reportValidity();
    }
  });
  area.addEventListener("input", function () { area.setCustomValidity(""); });
})();
