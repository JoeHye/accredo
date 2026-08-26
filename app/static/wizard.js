let applicationId = null;
let currentSectionCode = null;
let currentSubsectionCode = null;

document.addEventListener("DOMContentLoaded", async () => {
    try {
        const authRes = await fetch("/api/auth/me");
        if (!authRes.ok) {
            window.location.href = "/login";
            return;
        }
        await initializeApplication();
    } catch (err) {
        console.error("Initialization error:", err);
        document.getElementById("main-content").innerHTML = `<p style="color: red;">Error initializing wizard: ${err.message}</p>`;
    }
});

async function initializeApplication() {
    const appsRes = await fetch("/api/applications");
    if (!appsRes.ok) throw new Error("Failed to load user applications");
    const apps = await appsRes.json();

    if (!apps || apps.length === 0) {
        renderNoApplicationState();
        return;
    }

    applicationId = apps[0].id;
    document.getElementById("app-id-display").textContent = applicationId;

    loadCompleteness();
    loadSubsections();
}

function renderNoApplicationState() {
    document.getElementById("app-id-display").textContent = "-";
    document.getElementById("subsections-sidebar").innerHTML = "<h3>Application Sections</h3><p style='padding: 10px; font-size: 13px; color: #64748b;'>No sections available.</p>";

    const panel = document.getElementById("main-content");
    panel.innerHTML = `
        <div style="text-align: center; margin-top: 50px;">
            <h2>No Application Found</h2>
            <p style="color: #64748b; margin-bottom: 20px;">You do not have any accreditation applications started yet.</p>
            <button onclick="startNewApplication()" style="padding: 12px 24px; background: #3b82f6; color: white; border: none; border-radius: 6px; font-size: 15px; font-weight: bold; cursor: pointer; transition: background 0.2s;">Start New Application</button>
            <div id="create-app-status" style="margin-top: 15px; font-size: 13px;"></div>
        </div>
    `;
}

async function startNewApplication() {
    const statusDiv = document.getElementById("create-app-status");
    statusDiv.style.color = "#ca8a04";
    statusDiv.textContent = "Creating new application...";

    try {
        const response = await fetch("/api/applications", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({})
        });

        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.error || result.message || "Failed to create application");
        }

        statusDiv.style.color = "#16a34a";
        statusDiv.textContent = "Application created successfully! Loading wizard...";

        setTimeout(() => {
            window.location.reload();
        }, 600);

    } catch (err) {
        statusDiv.style.color = "#dc2626";
        statusDiv.textContent = `Failed to create application: ${err.message}`;
    }
}

async function logout() {
    try {
        const response = await fetch("/api/auth/logout", { method: "POST" });
        if (response.ok) {
            window.location.href = "/login";
        } else {
            alert("Logout failed");
        }
    } catch (err) {
        console.error("Logout error:", err);
        alert("Logout failed");
    }
}

async function loadCompleteness() {
    if (!applicationId) return;
    try {
        const response = await fetch(`/api/applications/${applicationId}/completeness`);
        if (!response.ok) throw new Error("Failed to load completeness");
        const data = await response.json();
        document.getElementById("completeness-pct").textContent = `${data.percentage}%`;
        document.getElementById("progress-bar").style.width = `${data.percentage}%`;
        document.getElementById("progress-text").textContent = `${data.completed} / ${data.total_required} Completed`;
    } catch (err) {
        console.error("Error loading completeness:", err);
    }
}

async function loadSubsections() {
    if (!applicationId) return;
    try {
        const response = await fetch(`/api/applications/${applicationId}/subsections`);
        if (!response.ok) throw new Error("Failed to load subsections");
        const subsections = await response.json();
        const sidebar = document.getElementById("subsections-sidebar");
        sidebar.innerHTML = "<h3>Application Sections</h3>";
        subsections.forEach(sub => {
            const div = document.createElement("div");
            div.className = `subsection-item ${currentSectionCode === sub.section_code && currentSubsectionCode === sub.subsection_code ? 'active' : ''}`;
            div.onclick = () => selectSubsection(sub.section_code, sub.subsection_code);
            const statusText = sub.is_not_applicable ? "N/A" : `${sub.answered_count} / ${sub.criterion_count} answered`;
            div.innerHTML = `
                <div class="subsection-title">${sub.subsection_code}: ${sub.subsection_name}</div>
                <div class="subsection-meta">
                    <span>Sec ${sub.section_code}</span>
                    <span style="font-weight: bold; color: ${sub.is_not_applicable ? '#d97706' : (sub.answered_count === sub.criterion_count ? '#16a34a' : '#475569')}">${statusText}</span>
                </div>
            `;
            sidebar.appendChild(div);
        });
    } catch (err) {
        console.error("Error loading subsections:", err);
    }
}

async function selectSubsection(sectionCode, subsectionCode) {
    if (!applicationId) return;
    currentSectionCode = sectionCode;
    currentSubsectionCode = subsectionCode;
    loadSubsections();
    const panel = document.getElementById("main-content");
    panel.innerHTML = "<p>Loading criteria...</p>";
    try {
        const response = await fetch(`/api/applications/${applicationId}/subsections/${sectionCode}/${subsectionCode}`);
        if (!response.ok) throw new Error("Failed to load subsection criteria");
        const data = await response.json();
        renderSubsectionContent(data);
    } catch (err) {
        panel.innerHTML = `<p style="color: red;">Error loading subsection: ${err.message}</p>`;
    }
}

function renderSubsectionContent(data) {
    const panel = document.getElementById("main-content");
    panel.innerHTML = "";
    const header = document.createElement("h2");
    header.textContent = `${data.subsection_code}: Subsection Criteria`;
    panel.appendChild(header);
    const naBanner = document.createElement("div");
    naBanner.className = "na-banner";
    naBanner.innerHTML = `
        <input type="checkbox" id="na-checkbox" ${data.is_not_applicable ? 'checked' : ''} onchange="toggleNotApplicable(this)">
        <label for="na-checkbox">Mark this entire subsection as Not Applicable (N/A)</label>
    `;
    panel.appendChild(naBanner);
    if (data.criteria.length === 0) {
        panel.innerHTML += "<p>No criteria found in this subsection.</p>";
        return;
    }
    data.criteria.forEach(crit => {
        panel.appendChild(createCriterionCard(crit));
    });
}

function createCriterionCard(crit) {
    const card = document.createElement("div");
    card.className = "criterion-card";
    let subCriteriaHtml = "";
    if (crit.sub_criteria && crit.sub_criteria.length > 0) {
        subCriteriaHtml = `<div class="sub-criteria-container">`;
        crit.sub_criteria.forEach(sc => {
            subCriteriaHtml += `
                <div class="criterion-header">Item ${sc.item_number}</div>
                <div class="question-text">${sc.question_text}</div>
                <textarea id="response-${sc.id}" onblur="saveResponse('${sc.id}')">${sc.response.narrative_text || ""}</textarea>
                <div id="status-${sc.id}" class="save-indicator"></div>
            `;
        });
        subCriteriaHtml += `</div>`;
    }
    card.innerHTML = `
        <div class="criterion-header">Criterion ${crit.item_number} (${crit.id})</div>
        <div class="question-text">${crit.question_text}</div>
        <textarea id="response-${crit.id}" onblur="saveResponse('${crit.id}')">${crit.response.narrative_text || ""}</textarea>
        <div id="status-${crit.id}" class="save-indicator"></div>
        ${subCriteriaHtml}
    `;
    return card;
}

async function saveResponse(criterionId) {
    if (!applicationId) return;
    const textarea = document.getElementById(`response-${criterionId}`);
    const statusDiv = document.getElementById(`status-${criterionId}`);
    const narrativeText = textarea.value;
    statusDiv.className = "save-indicator save-saving";
    statusDiv.textContent = "Saving...";
    try {
        const response = await fetch(`/api/applications/${applicationId}/responses/${criterionId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ narrative_text: narrativeText })
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Save failed");
        statusDiv.className = "save-indicator save-success";
        statusDiv.textContent = `Saved at ${new Date(result.saved_at).toLocaleTimeString()}`;
        loadCompleteness();
        loadSubsections();
    } catch (err) {
        statusDiv.className = "save-indicator save-error";
        statusDiv.textContent = `Save failed — retry (${err.message})`;
    }
}

async function toggleNotApplicable(checkbox) {
    if (!applicationId || !currentSectionCode || !currentSubsectionCode) return;
    const isNa = checkbox.checked;
    try {
        const response = await fetch(`/api/applications/${applicationId}/subsections/${currentSectionCode}/${currentSubsectionCode}/not-applicable`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ not_applicable: isNa })
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Failed to update subsection N/A status");
        selectSubsection(currentSectionCode, currentSubsectionCode);
        loadCompleteness();
    } catch (err) {
        alert(`Error updating Not Applicable status: ${err.message}`);
        checkbox.checked = !isNa;
    }
}

async function generateReport() {
    if (!applicationId) return;
    const btn = document.getElementById("generate-report-btn");
    const statusDiv = document.getElementById("report-status");
    btn.disabled = true;
    btn.textContent = "Generating...";
    statusDiv.style.color = "#fbbf24";
    statusDiv.textContent = "Generating report PDF...";
    try {
        const response = await fetch(`/api/applications/${applicationId}/report/generate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" }
        });
        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.error || result.details || "Report generation failed");
        }
        btn.disabled = false;
        btn.textContent = "Generate Report";
        statusDiv.style.color = "#4ade80";
        statusDiv.innerHTML = `Ready (${result.completeness_percentage}% complete) — <a href="/api/applications/${applicationId}/report/download" style="color: #60a5fa; text-decoration: underline; font-weight: bold;">Download Report</a>`;
    } catch (err) {
        btn.disabled = false;
        btn.textContent = "Generate Report";
        statusDiv.style.color = "#f87171";
        statusDiv.textContent = `Failed: ${err.message}`;
    }
}
