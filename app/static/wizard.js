// app/static/wizard.js
let applicationId = null;
let currentSectionCode = null;
let currentSubsectionCode = null;
let allUserApplications = [];
let isCurrentAppEmpty = true;
let currentApplicationDueDate = null;
let currentSubsectionsList = [];
const recentlyClonedCriterionIds = new Set();

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
    allUserApplications = apps || [];

    if (!apps || apps.length === 0) {
        renderNoApplicationState();
        return;
    }

    applicationId = apps[0].id;
    document.getElementById("app-id-display").textContent = applicationId;

    const currentApp = apps.find(a => a.id === applicationId);
    if (currentApp) {
        currentApplicationDueDate = currentApp.due_date || null;
    }
    renderDueDateDisplay();

    await loadCompleteness();
    await loadSubsections();
}

function renderNoApplicationState() {
    document.getElementById("app-id-display").textContent = "-";
    document.getElementById("subsections-sidebar").innerHTML = "<h3>Application Sections</h3><p style='padding: 10px; font-size: 13px; color: #64748b;'>No sections available.</p>";

    const panel = document.getElementById("main-content");
    panel.innerHTML = `
        <div style="text-align: center; margin-top: 50px;">
            <h2>No Application Found</h2>
            <p style="color: #64748b; margin-bottom: 20px;">You do not have any accreditation applications started yet.</p>
            <div id="start-app-container">
                <button onclick="showStartChoice()" style="padding: 12px 24px; background: #3b82f6; color: white; border: none; border-radius: 6px; font-size: 15px; font-weight: bold; cursor: pointer; transition: background 0.2s;">Start New Application</button>
            </div>
            <div id="create-app-status" style="margin-top: 15px; font-size: 13px;"></div>
        </div>
    `;
}

function showStartChoice() {
    const container = document.getElementById("start-app-container");
    if (!container) return;

    container.innerHTML = `
        <div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 20px; border-radius: 8px; display: inline-block; text-align: left; max-width: 450px;">
            <p style="font-weight: bold; color: #1e293b; margin-top: 0; font-size: 16px;">How would you like to start?</p>
            <p style="font-size: 13px; color: #64748b; margin-bottom: 20px; line-height: 1.4;">Answer a few quick questions and we'll pre-fill your cover page and skip sections that don't apply to you — takes about 2 minutes.</p>
            <div style="display: flex; gap: 10px;">
                <button onclick="startBlankApplicationPrompt()" style="flex: 1; padding: 10px 16px; background: #e2e8f0; color: #334155; border: none; border-radius: 6px; font-size: 13px; font-weight: bold; cursor: pointer;">Blank Application</button>
                <button onclick="showGuidedSetupForm()" style="flex: 1; padding: 10px 16px; background: #3b82f6; color: white; border: none; border-radius: 6px; font-size: 13px; font-weight: bold; cursor: pointer;">Guided Setup</button>
            </div>
        </div>
    `;
}

async function startBlankApplicationPrompt(selectedPackId = null) {
    await startNewApplication(selectedPackId, null);
}

function showGuidedSetupForm(selectedPackId = null) {
    const container = document.getElementById("start-app-container");
    if (!container) return;

    container.innerHTML = `
        <div style="background: #f8fafc; border: 1px solid #e2e8f0; padding: 20px; border-radius: 8px; display: inline-block; text-align: left; max-width: 480px; width: 100%; box-sizing: border-box;">
            <p style="font-weight: bold; color: #1e293b; margin-top: 0; font-size: 16px;">Guided Setup</p>
            
            <div style="margin-bottom: 12px;">
                <label style="display: block; font-size: 12px; font-weight: bold; color: #334155; margin-bottom: 4px;">Institution Name</label>
                <input type="text" id="setup-inst-name" style="width: 100%; padding: 8px; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 13px; box-sizing: border-box;" placeholder="e.g. Acme Technical Institute">
            </div>

            <div style="margin-bottom: 12px;">
                <label style="display: block; font-size: 12px; font-weight: bold; color: #334155; margin-bottom: 4px;">Founding Year</label>
                <input type="number" id="setup-founding-year" style="width: 100%; padding: 8px; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 13px; box-sizing: border-box;" placeholder="e.g. 2018">
            </div>

            <div style="margin-bottom: 15px;">
                <label style="display: block; font-size: 12px; font-weight: bold; color: #334155; margin-bottom: 4px;">Mission Statement (Optional)</label>
                <textarea id="setup-mission" style="width: 100%; height: 80px; padding: 8px; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 13px; resize: vertical; box-sizing: border-box;" placeholder="Enter institution mission..."></textarea>
            </div>

            <div style="margin-bottom: 15px; border-top: 1px solid #e2e8f0; paddingTop: 12px;">
                <label style="display: block; margin-bottom: 6px; cursor: pointer; color: #334155; font-size: 13px;">
                    <input type="checkbox" id="setup-dist-ed" style="margin-right: 8px;"> We offer distance education courses
                </label>
                <label style="display: block; margin-bottom: 6px; cursor: pointer; color: #334155; font-size: 13px;">
                    <input type="checkbox" id="setup-esl" style="margin-right: 8px;"> We offer ESL courses
                </label>
                <label style="display: block; margin-bottom: 6px; cursor: pointer; color: #334155; font-size: 13px;">
                    <input type="checkbox" id="setup-grad" style="margin-right: 8px;"> We offer graduate degree programs
                </label>
            </div>

            <div style="display: flex; gap: 10px;">
                <button onclick="submitGuidedSetup('${selectedPackId || ""}')" style="padding: 10px 20px; background: #3b82f6; color: white; border: none; border-radius: 6px; font-size: 13px; font-weight: bold; cursor: pointer;">Continue</button>
                <button onclick="renderNoApplicationState()" style="padding: 10px 16px; background: #e2e8f0; color: #334155; border: none; border-radius: 6px; font-size: 13px; font-weight: bold; cursor: pointer;">Back</button>
            </div>
        </div>
    `;
}

async function submitGuidedSetup(packId) {
    const instName = document.getElementById("setup-inst-name").value;
    const foundingYearVal = document.getElementById("setup-founding-year").value;
    const mission = document.getElementById("setup-mission").value;
    const distEd = document.getElementById("setup-dist-ed").checked;
    const esl = document.getElementById("setup-esl").checked;
    const grad = document.getElementById("setup-grad").checked;

    const onboarding = {
        institution_name: instName ? instName : null,
        founding_year: foundingYearVal ? parseInt(foundingYearVal, 10) : null,
        mission_statement: mission ? mission : null,
        offers_distance_education: distEd,
        offers_esl: esl,
        offers_graduate_degrees: grad
    };

    await startNewApplication(packId ? packId : null, onboarding);
}

async function startNewApplication(selectedPackId = null, onboardingObj = null) {
    const statusDiv = document.getElementById("create-app-status");
    if (statusDiv) {
        statusDiv.style.color = "#ca8a04";
        statusDiv.textContent = "Creating new application...";
    }

    try {
        const payload = {};
        if (selectedPackId) payload.standard_pack_id = selectedPackId;
        if (onboardingObj) payload.onboarding = onboardingObj;

        const response = await fetch("/api/applications", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });

        const result = await response.json();
        
        if (response.status === 400 && result.available_standard_packs) {
            if (statusDiv) statusDiv.textContent = "";
            const startContainer = document.getElementById("start-app-container") || document.getElementById("main-content");
            
            let formHtml = `
                <div style="margin-top: 20px; text-align: left; display: inline-block; background: #f8fafc; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
                    <p style="font-weight: bold; margin-bottom: 12px; color: #1e293b;">Select an Accreditation Standard Pack:</p>
            `;
            
            result.available_standard_packs.forEach((pack, idx) => {
                const checked = idx === 0 ? "checked" : "";
                const appTypeDisplay = pack.application_type ? pack.application_type.charAt(0).toUpperCase() + pack.application_type.slice(1) : "";
                const labelText = `${pack.accrediting_body} — ${appTypeDisplay}`;
                formHtml += `
                    <label style="display: block; margin-bottom: 8px; cursor: pointer; color: #334155; font-size: 14px;">
                        <input type="radio" name="standard_pack_choice" value="${pack.id}" ${checked} style="margin-right: 8px;">
                        ${labelText}
                    </label>
                `;
            });

            formHtml += `
                    <button type="button" onclick="submitPackPicker()" style="margin-top: 15px; padding: 10px 20px; background: #3b82f6; color: white; border: none; border-radius: 6px; font-size: 14px; font-weight: bold; cursor: pointer;">Continue</button>
                </div>
            `;
            
            startContainer.innerHTML = formHtml;
            return;
        }

        if (!response.ok) {
            throw new Error(result.error || result.message || "Failed to create application");
        }

        if (statusDiv) {
            statusDiv.style.color = "#16a34a";
            statusDiv.textContent = "Application created successfully! Loading wizard...";
        }

        setTimeout(() => {
            window.location.reload();
        }, 600);

    } catch (err) {
        if (statusDiv) {
            statusDiv.style.color = "#dc2626";
            statusDiv.textContent = `Failed to create application: ${err.message}`;
        }
    }
}

function submitPackPicker() {
    const selectedRadio = document.querySelector('input[name="standard_pack_choice"]:checked');
    if (!selectedRadio) {
        alert("Please select a standard pack.");
        return;
    }
    startBlankApplicationPrompt(selectedRadio.value);
}

function toggleDueDateEditor() {
    const displaySpan = document.getElementById("due-date-display");
    const setBtn = document.getElementById("set-due-date-btn");
    const editorDiv = document.getElementById("due-date-editor");
    const input = document.getElementById("due-date-input");

    if (editorDiv.style.display === "none" || !editorDiv.style.display) {
        editorDiv.style.display = "flex";
        if (setBtn) setBtn.style.display = "none";
        if (displaySpan) displaySpan.style.display = "none";
        if (currentApplicationDueDate && input) {
            input.value = currentApplicationDueDate;
        }
    } else {
        editorDiv.style.display = "none";
        if (setBtn) setBtn.style.display = "inline";
        if (displaySpan) displaySpan.style.display = "inline";
    }
}

async function saveDueDate() {
    if (!applicationId) return;
    const input = document.getElementById("due-date-input");
    const statusSpan = document.getElementById("due-date-status");
    if (!input) return;

    const dateVal = input.value ? input.value : null;
    statusSpan.style.color = "#d97706";
    statusSpan.textContent = "Saving...";

    try {
        const response = await fetch(`/api/applications/${applicationId}/due-date`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ due_date: dateVal })
        });

        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.error || result.message || "Failed to update due date");
        }

        currentApplicationDueDate = result.due_date;
        renderDueDateDisplay();
        toggleDueDateEditor();
        statusSpan.textContent = "";

        const appsRes = await fetch("/api/applications");
        if (appsRes.ok) {
            allUserApplications = await appsRes.json() || [];
        }
    } catch (err) {
        statusSpan.style.color = "#dc2626";
        statusSpan.textContent = err.message;
    }
}

function renderDueDateDisplay() {
    const displaySpan = document.getElementById("due-date-display");
    const setBtn = document.getElementById("set-due-date-btn");
    if (!displaySpan) return;

    if (!currentApplicationDueDate) {
        displaySpan.textContent = "No due date set";
        displaySpan.style.color = "#334155";
        if (setBtn) setBtn.textContent = "Set due date";
        return;
    }

    const today = new Date();
    today.setHours(0, 0, 0, 0);

    const due = new Date(currentApplicationDueDate + "T00:00:00");
    const diffTime = due - today;
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));

    if (diffDays > 14) {
        displaySpan.textContent = `Due in ${diffDays} days (${currentApplicationDueDate})`;
        displaySpan.style.color = "#16a34a";
    } else if (diffDays >= 1 && diffDays <= 14) {
        displaySpan.textContent = `Due in ${diffDays} days (${currentApplicationDueDate})`;
        displaySpan.style.color = "#d97706";
    } else if (diffDays === 0) {
        displaySpan.textContent = `Due today (${currentApplicationDueDate})`;
        displaySpan.style.color = "#dc2626";
    } else {
        const overdueDays = Math.abs(diffDays);
        displaySpan.textContent = `Overdue by ${overdueDays} days (${currentApplicationDueDate})`;
        displaySpan.style.color = "#dc2626";
    }

    if (setBtn) setBtn.textContent = "Edit";
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
        
        isCurrentAppEmpty = (data.completed === 0);
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
        currentSubsectionsList = subsections || [];
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

    const otherApps = allUserApplications.filter(a => a.id !== applicationId);
    if (isCurrentAppEmpty && otherApps.length > 0) {
        const cloneBanner = document.createElement("div");
        cloneBanner.id = "clone-from-banner";
        cloneBanner.style.cssText = "background: #eff6ff; border: 1px solid #bfdbfe; padding: 12px 16px; border-radius: 6px; margin-bottom: 20px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;";
        
        let selectOptions = otherApps.map(a => `<option value="${a.id}">Application #${a.id} (${a.status})</option>`).join("");
        cloneBanner.innerHTML = `
            <div>
                <strong style="color: #1e40af; font-size: 13px;">📋 Clone from previous application:</strong>
                <span style="color: #3b82f6; font-size: 13px; margin-left: 6px;">Pre-fill matching narrative responses from a prior submission.</span>
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
                <select id="source-app-select" style="padding: 6px 10px; border: 1px solid #93c5fd; border-radius: 4px; font-size: 13px;">
                    ${selectOptions}
                </select>
                <button type="button" id="clone-submit-btn" onclick="executeCloneFrom()" style="padding: 6px 14px; background: #2563eb; color: white; border: none; border-radius: 4px; font-size: 13px; font-weight: bold; cursor: pointer;">Clone Responses</button>
            </div>
        `;
        panel.appendChild(cloneBanner);
    }

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

    const copySources = [];
    function collectSources(criterionList) {
        criterionList.forEach(c => {
            if (c.answer_type === "narrative" && c.response && c.response.narrative_text && c.response.narrative_text.trim() !== "") {
                copySources.push(c);
            }
            if (c.sub_criteria && c.sub_criteria.length > 0) {
                collectSources(c.sub_criteria);
            }
        });
    }
    collectSources(data.criteria);

    data.criteria.forEach(crit => {
        panel.appendChild(createCriterionCard(crit, copySources));
    });

    let currentIndex = currentSubsectionsList.findIndex(sub => sub.section_code === currentSectionCode && sub.subsection_code === currentSubsectionCode);
    let prevSub = currentIndex > 0 ? currentSubsectionsList[currentIndex - 1] : null;
    let nextSub = currentIndex >= 0 && currentIndex < currentSubsectionsList.length - 1 ? currentSubsectionsList[currentIndex + 1] : null;

    const navContainer = document.createElement("div");
    navContainer.style.cssText = "display: flex; justify-content: space-between; margin-top: 30px; padding-top: 20px; border-top: 1px solid #e2e8f0;";
    
    const prevBtn = document.createElement("button");
    prevBtn.textContent = "← Previous Subsection";
    prevBtn.style.cssText = `padding: 8px 16px; background: #e2e8f0; color: #334155; border: none; border-radius: 6px; font-size: 13px; font-weight: bold; cursor: ${prevSub ? 'pointer' : 'not-allowed'}; opacity: ${prevSub ? '1' : '0.5'};`;
    prevBtn.disabled = !prevSub;
    if (prevSub) {
        prevBtn.onclick = () => selectSubsection(prevSub.section_code, prevSub.subsection_code);
    }

    const nextBtn = document.createElement("button");
    nextBtn.textContent = "Next Subsection →";
    nextBtn.style.cssText = `padding: 8px 16px; background: #3b82f6; color: white; border: none; border-radius: 6px; font-size: 13px; font-weight: bold; cursor: ${nextSub ? 'pointer' : 'not-allowed'}; opacity: ${nextSub ? '1' : '0.5'};`;
    nextBtn.disabled = !nextSub;
    if (nextSub) {
        nextBtn.onclick = () => selectSubsection(nextSub.section_code, nextSub.subsection_code);
    }

    navContainer.appendChild(prevBtn);
    navContainer.appendChild(nextBtn);
    panel.appendChild(navContainer);
}

async function executeCloneFrom() {
    const select = document.getElementById("source-app-select");
    const btn = document.getElementById("clone-submit-btn");
    if (!select || !btn) return;

    const sourceAppId = select.value;
    btn.disabled = true;
    btn.textContent = "Cloning...";

    try {
        const response = await fetch(`/api/applications/${applicationId}/clone-from/${sourceAppId}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" }
        });

        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.error || result.message || "Cloning failed");
        }

        if (result.cloned_criteria) {
            result.cloned_criteria.forEach(c => recentlyClonedCriterionIds.add(c.target_criterion_id));
        }

        alert(`Cloned ${result.total_cloned} matching narrative response(s) from Application #${sourceAppId}. Please review all copied answers.`);

        await loadCompleteness();
        await loadSubsections();
        if (currentSectionCode && currentSubsectionCode) {
            selectSubsection(currentSectionCode, currentSubsectionCode);
        }
    } catch (err) {
        alert(`Cloning failed: ${err.message}`);
        btn.disabled = false;
        btn.textContent = "Clone Responses";
    }
}

function createCriterionCard(crit, copySources) {
    const card = document.createElement("div");
    card.className = "criterion-card";
    let subCriteriaHtml = "";

    if (crit.sub_criteria && crit.sub_criteria.length > 0) {
        subCriteriaHtml = `<div class="sub-criteria-container">`;
        crit.sub_criteria.forEach(sc => {
            const isScCloned = recentlyClonedCriterionIds.has(sc.id);
            const scClonedBadgeHtml = isScCloned ? `
                <div style="background: #fef3c7; color: #92400e; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-bottom: 6px; display: inline-block;">
                    📋 Copied from prior application — please review
                </div>
            ` : "";

            if (sc.answer_type === "table_rows") {
                subCriteriaHtml += `
                    <div class="criterion-header">Item ${sc.item_number}</div>
                    ${scClonedBadgeHtml}
                    <div class="question-text">${sc.question_text}</div>
                    <div id="table-container-${sc.id}">Loading table...</div>
                    <div id="status-${sc.id}" class="save-indicator"></div>
                `;
                setTimeout(() => loadTableRows(applicationId, sc.id, sc.table_columns), 0);
            } else if (sc.answer_type === "exhibit") {
                subCriteriaHtml += `
                    <div class="criterion-header">Item ${sc.item_number}</div>
                    ${scClonedBadgeHtml}
                    <div class="question-text">${sc.question_text}</div>
                    <div id="exhibit-container-${sc.id}">Loading attachments...</div>
                    <div id="status-${sc.id}" class="save-indicator"></div>
                `;
                setTimeout(() => loadExhibits(applicationId, sc.id), 0);
            } else {
                const draftButtonHtml = sc.answer_type === "narrative" ? `
                    <button type="button" id="draft-btn-${sc.id}" onclick="draftAnswer('${sc.id}')" style="margin-top: 6px; padding: 4px 10px; background: #8b5cf6; color: white; border: none; border-radius: 4px; font-size: 12px; font-weight: bold; cursor: pointer;">Draft this answer</button>
                    <span id="draft-error-${sc.id}" style="margin-left: 8px; font-size: 12px; color: #dc2626;"></span>
                ` : "";

                let copyPickerHtml = "";
                if (sc.answer_type === "narrative") {
                    const validSources = copySources.filter(s => s.id !== sc.id);
                    if (validSources.length > 0) {
                        let optionsHtml = `<option value="">Copy from...</option>`;
                        validSources.forEach(src => {
                            let excerpt = src.question_text || "";
                            if (excerpt.length > 50) {
                                excerpt = excerpt.substring(0, 47) + "...";
                            }
                            optionsHtml += `<option value="${src.id}">Item ${src.item_number}: ${excerpt}</option>`;
                        });
                        copyPickerHtml = `
                            <div style="margin-top: 6px; display: flex; gap: 8px; align-items: center;">
                                <select id="copy-source-${sc.id}" style="padding: 4px 8px; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 12px;">
                                    ${optionsHtml}
                                </select>
                                <button type="button" onclick="copyAnswer('${sc.id}')" style="padding: 4px 10px; background: #475569; color: white; border: none; border-radius: 4px; font-size: 12px; font-weight: bold; cursor: pointer;">Copy</button>
                            </div>
                        `;
                    }
                }

                subCriteriaHtml += `
                    <div class="criterion-header">Item ${sc.item_number}</div>
                    ${scClonedBadgeHtml}
                    <div class="question-text">${sc.question_text}</div>
                    <textarea id="response-${sc.id}" onblur="saveResponse('${sc.id}')">${sc.response.narrative_text || ""}</textarea>
                    <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                        ${draftButtonHtml}
                        ${copyPickerHtml}
                    </div>
                    <div id="status-${sc.id}" class="save-indicator"></div>
                `;
            }
        });
        subCriteriaHtml += `</div>`;
    }

    const isTopCloned = recentlyClonedCriterionIds.has(crit.id);
    const topClonedBadgeHtml = isTopCloned ? `
        <div style="background: #fef3c7; color: #92400e; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-bottom: 6px; display: inline-block;">
            📋 Copied from prior application — please review
        </div>
    ` : "";

    if (crit.answer_type === "table_rows") {
        card.innerHTML = `
            <div class="criterion-header">Criterion ${crit.item_number} (${crit.id})</div>
            ${topClonedBadgeHtml}
            <div class="question-text">${crit.question_text}</div>
            <div id="table-container-${crit.id}">Loading table...</div>
            <div id="status-${crit.id}" class="save-indicator"></div>
            ${subCriteriaHtml}
        `;
        setTimeout(() => loadTableRows(applicationId, crit.id, crit.table_columns), 0);
    } else if (crit.answer_type === "exhibit") {
        card.innerHTML = `
            <div class="criterion-header">Criterion ${crit.item_number} (${crit.id})</div>
            ${topClonedBadgeHtml}
            <div class="question-text">${crit.question_text}</div>
            <div id="exhibit-container-${crit.id}">Loading attachments...</div>
            <div id="status-${crit.id}" class="save-indicator"></div>
            ${subCriteriaHtml}
        `;
        setTimeout(() => loadExhibits(applicationId, crit.id), 0);
    } else {
        const topDraftButtonHtml = crit.answer_type === "narrative" ? `
            <button type="button" id="draft-btn-${crit.id}" onclick="draftAnswer('${crit.id}')" style="margin-top: 6px; padding: 4px 10px; background: #8b5cf6; color: white; border: none; border-radius: 4px; font-size: 12px; font-weight: bold; cursor: pointer;">Draft this answer</button>
            <span id="draft-error-${crit.id}" style="margin-left: 8px; font-size: 12px; color: #dc2626;"></span>
        ` : "";

        let topCopyPickerHtml = "";
        if (crit.answer_type === "narrative") {
            const validSources = copySources.filter(s => s.id !== crit.id);
            if (validSources.length > 0) {
                let optionsHtml = `<option value="">Copy from...</option>`;
                validSources.forEach(src => {
                    let excerpt = src.question_text || "";
                    if (excerpt.length > 50) {
                        excerpt = excerpt.substring(0, 47) + "...";
                    }
                    optionsHtml += `<option value="${src.id}">Item ${src.item_number}: ${excerpt}</option>`;
                });
                topCopyPickerHtml = `
                    <div style="margin-top: 6px; display: flex; gap: 8px; align-items: center;">
                        <select id="copy-source-${crit.id}" style="padding: 4px 8px; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 12px;">
                            ${optionsHtml}
                        </select>
                        <button type="button" onclick="copyAnswer('${crit.id}')" style="padding: 4px 10px; background: #475569; color: white; border: none; border-radius: 4px; font-size: 12px; font-weight: bold; cursor: pointer;">Copy</button>
                    </div>
                `;
            }
        }

        card.innerHTML = `
            <div class="criterion-header">Criterion ${crit.item_number} (${crit.id})</div>
            ${topClonedBadgeHtml}
            <div class="question-text">${crit.question_text}</div>
            <textarea id="response-${crit.id}" onblur="saveResponse('${crit.id}')">${crit.response.narrative_text || ""}</textarea>
            <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                ${topDraftButtonHtml}
                ${topCopyPickerHtml}
            </div>
            <div id="status-${crit.id}" class="save-indicator"></div>
            ${subCriteriaHtml}
        `;
    }
    return card;
}

async function loadExhibits(appId, criterionId) {
    const container = document.getElementById(`exhibit-container-${criterionId}`);
    if (!container) return;

    try {
        const res = await fetch(`/api/applications/${appId}/responses/${criterionId}/exhibit`);
        if (!res.ok) throw new Error("Failed to load exhibits");
        const files = await res.json();
        renderExhibitEditor(appId, criterionId, files || []);
    } catch (err) {
        container.innerHTML = `<p style="color: red; font-size: 12px;">Error loading attachments: ${err.message}</p>`;
    }
}

function renderExhibitEditor(appId, criterionId, files) {
    const container = document.getElementById(`exhibit-container-${criterionId}`);
    if (!container) return;

    let html = "";
    if (files.length === 0) {
        html += `<div style="font-size: 13px; color: #dc2626; font-weight: bold; margin-bottom: 8px;">[EXHIBIT REQUIRED]</div>`;
    } else {
        html += `<div style="margin-bottom: 10px; display: flex; flex-direction: column; gap: 6px;">`;
        files.forEach(f => {
            html += `
                <div style="display: flex; justify-content: space-between; align-items: center; background: #f1f5f9; padding: 6px 10px; border-radius: 4px; border: 1px solid #cbd5e1; font-size: 13px;">
                    <span>📎 <b>${escapeHtml(f.filename)}</b> (${Math.round(f.file_size_bytes / 1024)} KB)</span>
                    <button type="button" onclick="deleteExhibit('${appId}', '${criterionId}', ${f.id})" style="background: none; border: none; color: #dc2626; cursor: pointer; font-weight: bold; font-size: 12px;">Delete</button>
                </div>
            `;
        });
        html += `</div>`;
    }

    html += `
        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <input type="file" id="exhibit-file-${criterionId}" accept=".pdf,.doc,.docx,.jpg,.jpeg,.png" style="font-size: 12px;">
            <button type="button" onclick="uploadExhibit('${appId}', '${criterionId}')" style="padding: 6px 12px; background: #3b82f6; color: white; border: none; border-radius: 4px; font-size: 12px; font-weight: bold; cursor: pointer;">Upload</button>
        </div>
    `;

    container.innerHTML = html;
}

async function uploadExhibit(appId, criterionId) {
    const fileInput = document.getElementById(`exhibit-file-${criterionId}`);
    const statusDiv = document.getElementById(`status-${criterionId}`);
    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        alert("Please select a file to upload.");
        return;
    }

    const file = fileInput.files[0];
    const formData = new FormData();
    formData.append("file", file);

    if (statusDiv) {
        statusDiv.className = "save-indicator save-saving";
        statusDiv.textContent = "Uploading exhibit...";
    }

    try {
        const response = await fetch(`/api/applications/${appId}/responses/${criterionId}/exhibit`, {
            method: "POST",
            body: formData
        });

        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.error || result.details || "Exhibit upload failed");
        }

        if (statusDiv) {
            statusDiv.className = "save-indicator save-success";
            statusDiv.textContent = `Uploaded at ${new Date().toLocaleTimeString()}`;
        }

        await loadExhibits(appId, criterionId);
        await loadCompleteness();
        await loadSubsections();
    } catch (err) {
        if (statusDiv) {
            statusDiv.className = "save-indicator save-error";
            statusDiv.textContent = `Upload failed — ${err.message}`;
        } else {
            alert(`Upload failed: ${err.message}`);
        }
    }
}

async function deleteExhibit(appId, criterionId, evidenceId) {
    const confirmed = confirm("Delete this file?");
    if (!confirmed) return;

    const statusDiv = document.getElementById(`status-${criterionId}`);
    if (statusDiv) {
        statusDiv.className = "save-indicator save-saving";
        statusDiv.textContent = "Deleting exhibit...";
    }

    try {
        const response = await fetch(`/api/applications/${appId}/responses/${criterionId}/exhibit/${evidenceId}`, {
            method: "DELETE"
        });

        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.error || result.details || "Failed to delete exhibit");
        }

        if (statusDiv) {
            statusDiv.className = "save-indicator save-success";
            statusDiv.textContent = `Deleted at ${new Date().toLocaleTimeString()}`;
        }

        await loadExhibits(appId, criterionId);
        await loadCompleteness();
        await loadSubsections();
    } catch (err) {
        if (statusDiv) {
            statusDiv.className = "save-indicator save-error";
            statusDiv.textContent = `Delete failed — ${err.message}`;
        } else {
            alert(`Delete failed: ${err.message}`);
        }
    }
}

function copyAnswer(targetCriterionId) {
    const select = document.getElementById(`copy-source-${targetCriterionId}`);
    const textarea = document.getElementById(`response-${targetCriterionId}`);
    if (!select || !textarea) return;

    const sourceCriterionId = select.value;
    if (!sourceCriterionId) {
        alert("Please select a source criterion to copy from.");
        return;
    }

    const sourceTextarea = document.getElementById(`response-${sourceCriterionId}`);
    if (!sourceTextarea) {
        alert("Source response text not found.");
        return;
    }

    const sourceText = sourceTextarea.value;
    if (!sourceText.trim()) {
        alert("The selected source criterion has no text to copy.");
        return;
    }

    if (textarea.value.trim() !== "") {
        const confirmed = confirm("The textarea already contains text. Overwrite it with the copied text?");
        if (!confirmed) return;
    }

    textarea.value = sourceText;
}

async function draftAnswer(criterionId) {
    if (!applicationId) return;
    const btn = document.getElementById(`draft-btn-${criterionId}`);
    const textarea = document.getElementById(`response-${criterionId}`);
    const errSpan = document.getElementById(`draft-error-${criterionId}`);
    if (!btn || !textarea) return;

    if (textarea.value.trim() !== "") {
        const confirmed = confirm("The textarea already contains text. Overwrite it with the AI draft?");
        if (!confirmed) return;
    }

    btn.disabled = true;
    btn.textContent = "Drafting...";
    if (errSpan) errSpan.textContent = "";

    try {
        const response = await fetch(`/api/applications/${applicationId}/responses/${criterionId}/draft`, {
            method: "POST",
            headers: { "Content-Type": "application/json" }
        });

        const result = await response.json();
        if (!response.ok) {
            throw new Error(result.error || result.details || "AI drafting failed");
        }

        textarea.value = result.draft_text || "";
    } catch (err) {
        console.error("Drafting error:", err);
        if (errSpan) {
            errSpan.textContent = err.message;
        } else {
            alert(`Drafting failed: ${err.message}`);
        }
    } finally {
        btn.disabled = false;
        btn.textContent = "Draft this answer";
    }
}

async function loadTableRows(appId, criterionId, columns) {
    const container = document.getElementById(`table-container-${criterionId}`);
    if (!container) return;

    try {
        const res = await fetch(`/api/applications/${appId}/responses/${criterionId}/table-rows`);
        if (!res.ok) throw new Error("Failed to load table data");
        const data = await res.json();
        renderTableEditor(appId, criterionId, columns || data.table_columns || [], data.rows || []);
    } catch (err) {
        container.innerHTML = `<p style="color: red; font-size: 12px;">Error loading table: ${err.message}</p>`;
    }
}

function renderTableEditor(appId, criterionId, columns, rows) {
    const container = document.getElementById(`table-container-${criterionId}`);
    if (!container) return;

    let html = `
        <div style="overflow-x: auto; margin-bottom: 10px;">
            <table id="table-elem-${criterionId}" style="width: 100%; border-collapse: collapse; font-size: 13px;">
                <thead>
                    <tr style="background: #f1f5f9; border-bottom: 2px solid #cbd5e1;">
    `;
    columns.forEach(col => {
        html += `<th style="padding: 8px; text-align: left; border: 1px solid #cbd5e1; color: #334155;">${col}</th>`;
    });
    html += `<th style="padding: 8px; text-align: center; border: 1px solid #cbd5e1; width: 60px;">Action</th></tr></thead><tbody>`;

    if (rows.length === 0) {
        html += generateTableRowHtml(columns, {});
    } else {
        rows.forEach(r => {
            html += generateTableRowHtml(columns, r.row_data || {});
        });
    }

    html += `</tbody></table></div>`;
    html += `
        <div style="display: flex; gap: 10px; align-items: center;">
            <button type="button" onclick="addTableRow('${criterionId}', ${JSON.stringify(columns).replace(/"/g, '&quot;')})" style="padding: 6px 12px; background: #e2e8f0; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: bold; color: #334155;">+ Add Row</button>
            <button type="button" onclick="saveTableRows('${appId}', '${criterionId}', ${JSON.stringify(columns).replace(/"/g, '&quot;')})" style="padding: 6px 14px; background: #3b82f6; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: bold; color: white;">Save Table</button>
        </div>
    `;

    container.innerHTML = html;
}

function generateTableRowHtml(columns, rowData) {
    let rowHtml = `<tr style="border-bottom: 1px solid #e2e8f0;">`;
    columns.forEach(col => {
        const val = rowData[col] !== undefined ? rowData[col] : "";
        rowHtml += `<td style="padding: 6px; border: 1px solid #cbd5e1;"><input type="text" data-column="${col}" value="${escapeHtml(val)}" style="width: 100%; padding: 4px; border: 1px solid #cbd5e1; border-radius: 3px; font-size: 12px; box-sizing: border-box;" /></td>`;
    });
    rowHtml += `<td style="padding: 6px; border: 1px solid #cbd5e1; text-align: center;"><button type="button" onclick="this.closest('tr').remove()" style="background: none; border: none; color: #dc2626; cursor: pointer; font-weight: bold; font-size: 14px;" title="Delete Row">&times;</button></td>`;
    rowHtml += `</tr>`;
    return rowHtml;
}

function addTableRow(criterionId, columns) {
    const tbody = document.querySelector(`#table-elem-${criterionId} tbody`);
    if (!tbody) return;
    const tr = document.createElement("tr");
    tr.style.borderBottom = "1px solid #e2e8f0";
    let trHtml = "";
    columns.forEach(col => {
        trHtml += `<td style="padding: 6px; border: 1px solid #cbd5e1;"><input type="text" data-column="${col}" value="" style="width: 100%; padding: 4px; border: 1px solid #cbd5e1; border-radius: 3px; font-size: 12px; box-sizing: border-box;" /></td>`;
    });
    trHtml += `<td style="padding: 6px; border: 1px solid #cbd5e1; text-align: center;"><button type="button" onclick="this.closest('tr').remove()" style="background: none; border: none; color: #dc2626; cursor: pointer; font-weight: bold; font-size: 14px;" title="Delete Row">&times;</button></td>`;
    tr.innerHTML = trHtml;
    tbody.appendChild(tr);
}

async function saveTableRows(appId, criterionId, columns) {
    const statusDiv = document.getElementById(`status-${criterionId}`);
    const tbody = document.querySelector(`#table-elem-${criterionId} tbody`);
    if (!statusDiv || !tbody) return;

    statusDiv.className = "save-indicator save-saving";
    statusDiv.textContent = "Saving...";

    const rows = [];
    const trs = tbody.querySelectorAll("tr");
    trs.forEach(tr => {
        const rowData = {};
        columns.forEach(col => {
            const input = tr.querySelector(`input[data-column="${col}"]`);
            rowData[col] = input ? input.value : "";
        });
        rows.push(rowData);
    });

    try {
        const response = await fetch(`/api/applications/${appId}/responses/${criterionId}/table-rows`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ rows })
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

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
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
            headers: { "Content-Type": "application/json" },
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