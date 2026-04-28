let currentSocket = null;
let currentJobId = null;

function initForm() {
    const form = document.getElementById('extract-form');
    if (form && !form._listenerAttached) {
        form._listenerAttached = true;
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            startExtraction();
        });
    }
}

document.addEventListener('DOMContentLoaded', initForm);
window.addEventListener('load', initForm);  // fallback

function addLog(message, type = 'info') {
    const logsContainer = document.getElementById('logs');
    if (!logsContainer) return;
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;
    const time = new Date().toLocaleTimeString();
    entry.textContent = `[${time}] ${message}`;
    logsContainer.appendChild(entry);
    logsContainer.scrollTop = logsContainer.scrollHeight;
}

async function stopExtraction() {
    if (!currentJobId) return;
    const stopBtn = document.getElementById('stopBtn');
    try {
        await fetch(`/api/cancel/${currentJobId}`, { method: 'POST' });
        addLog("Cancellation requested. Stopping all active processes...", "error");
        stopBtn.disabled = true;
        stopBtn.innerHTML = 'Stopping...';
    } catch (e) {
        console.error("Failed to cancel", e);
    }
}

function copyLogs() {
    const logsContainer = document.getElementById('logs');
    const btn = document.getElementById('copyLogsBtn');
    const label = document.getElementById('copyLogsLabel');
    if (!logsContainer) return;

    const lines = Array.from(logsContainer.querySelectorAll('.log-entry'))
        .map(el => el.textContent.trim())
        .join('\n');

    navigator.clipboard.writeText(lines).then(() => {
        btn.classList.add('copied');
        label.textContent = '✓ Copied';
        setTimeout(() => {
            btn.classList.remove('copied');
            label.textContent = 'Copy';
        }, 2000);
    }).catch(() => {
        // Fallback for older browsers
        const ta = document.createElement('textarea');
        ta.value = lines;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        label.textContent = '✓ Copied';
        setTimeout(() => { label.textContent = 'Copy'; }, 2000);
    });
}


function renderResults(documents) {
    const resultsContainer = document.getElementById('results');
    if (!resultsContainer) return;
    
    if (!documents || documents.length === 0) {
        resultsContainer.innerHTML = `
            <div class="empty-state">
                <p>No documents found matching the criteria.</p>
            </div>
        `;
        return;
    }

    const metaKeys = new Set();
    documents.forEach(doc => {
        if (doc.metadata) {
            Object.keys(doc.metadata).forEach(key => metaKeys.add(key));
        }
    });
    const metaColumns = Array.from(metaKeys);

    let tableHtml = `
        <div class="table-responsive">
            <table class="results-table">
                <thead>
                    <tr>
                        <th>Title</th>
                        <th>Date</th>
                        ${metaColumns.map(col => `<th>${col}</th>`).join('')}
                        <th>Link</th>
                    </tr>
                </thead>
                <tbody>
    `;

    documents.forEach((doc, index) => {
        let metaCells = metaColumns.map(col => {
            const val = (doc.metadata && doc.metadata[col]) ? doc.metadata[col] : '-';
            return `<td>${val}</td>`;
        }).join('');

        const linkHtml = doc.local_url 
            ? `<a href="${doc.local_url}" target="_blank" download style="color: #10b981; font-weight: 600;">Download Local</a><br><a href="${doc.url}" target="_blank" style="font-size: 0.8rem; opacity: 0.7; margin-top: 0.25rem; display: inline-block;">Source</a>` 
            : (doc.url ? `<a href="${doc.url}" target="_blank">View</a>` : '-');

        tableHtml += `
            <tr style="animation-delay: ${index * 0.05}s">
                <td style="font-weight: 600;">${doc.title || '-'}</td>
                <td style="white-space: nowrap; color: #93c5fd;">${doc.date || '-'}</td>
                ${metaCells}
                <td>${linkHtml}</td>
            </tr>
        `;
    });

    tableHtml += `</tbody></table></div>`;
    resultsContainer.innerHTML = tableHtml;
}

function connectWebSocket(jobId) {
    const extractBtn = document.getElementById('extractBtn');
    const stopBtn = document.getElementById('stopBtn');
    const statusIndicator = document.getElementById('status-indicator');

    if (currentSocket) currentSocket.close();

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/${jobId}`;
    currentSocket = new WebSocket(wsUrl);

    currentSocket.onopen = () => {
        statusIndicator.classList.add('active');
        addLog("Connected to extraction engine...", "system");
    };

    currentSocket.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'log') {
                const type = data.message.includes('ERROR') ? 'error' : 
                             data.message.includes('successfully') || data.message.includes('finished') ? 'success' : 'info';
                addLog(data.message, type);
            } else if (data.type === 'cost') {
                document.getElementById('cost-tracker').textContent = `Cost: $${data.value.toFixed(6)}`;
            } else if (data.type === 'result') {
                renderResults(data.data);
                extractBtn.disabled = false;
                extractBtn.classList.remove('loading');
                statusIndicator.classList.remove('active');
                stopBtn.classList.add('hidden');
            }
        } catch (e) {
            console.error("Error parsing websocket message", e);
        }
    };

    currentSocket.onclose = () => {
        statusIndicator.classList.remove('active');
        extractBtn.disabled = false;
        extractBtn.classList.remove('loading');
        stopBtn.classList.add('hidden');
    };
}

async function startExtraction() {
    const extractBtn = document.getElementById('extractBtn');
    const stopBtn = document.getElementById('stopBtn');
    const resultsContainer = document.getElementById('results');
    const logsContainer = document.getElementById('logs');

    const url = document.getElementById('url').value;
    const start_date = document.getElementById('start_date').value;
    const end_date = document.getElementById('end_date').value;
    const engine = document.getElementById('engine').value;
    const model = document.getElementById('model').value;

    if (!url) return;

    // Reset UI
    document.getElementById('cost-tracker').textContent = 'Cost: $0.000000';
    extractBtn.disabled = true;
    extractBtn.classList.add('loading');
    stopBtn.classList.remove('hidden');
    stopBtn.disabled = false;
    stopBtn.innerHTML = 'Stop Process';
    logsContainer.innerHTML = '';
    resultsContainer.innerHTML = `
        <div class="empty-state">
            <div class="spinner" style="display: block; border-color: var(--accent); border-top-color: transparent;"></div>
            <p>Analyzing website structure and extracting data...</p>
        </div>
    `;
    
    addLog("Initiating extraction request...", "system");

    try {
        const response = await fetch('/api/extract', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, start_date, end_date, engine, model })
        });

        const data = await response.json();
        if (data.job_id) {
            currentJobId = data.job_id;
            addLog(`Job created. ID: ${data.job_id}`, "success");
            connectWebSocket(data.job_id);
        }
    } catch (error) {
        addLog(`Failed: ${error.message}`, "error");
        extractBtn.disabled = false;
        extractBtn.classList.remove('loading');
        stopBtn.classList.add('hidden');
    }
}
