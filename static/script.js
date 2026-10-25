// ============ CSRF 보호 (모든 상태 변경 요청에 토큰 자동 첨부) ============
(function () {
    const meta = document.querySelector('meta[name="csrf-token"]');
    const csrfToken = meta ? meta.getAttribute('content') : '';
    if (!csrfToken) return;
    const originalFetch = window.fetch;
    window.fetch = function (input, init) {
        const options = init ? Object.assign({}, init) : {};
        const method = (options.method || 'GET').toUpperCase();
        if (['POST', 'PUT', 'PATCH', 'DELETE'].indexOf(method) !== -1) {
            const headers = new Headers(options.headers || {});
            if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', csrfToken);
            options.headers = headers;
        }
        return originalFetch(input, options);
    };
})();

// ============ TAB NAVIGATION ============
document.querySelectorAll('.sidebar nav a').forEach(link => {
    link.addEventListener('click', (e) => {
        e.preventDefault();
        const tab = link.dataset.tab;
        
        document.querySelectorAll('.sidebar nav a').forEach(a => a.classList.remove('active'));
        link.classList.add('active');
        
        document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
        document.getElementById('tab-' + tab).classList.add('active');
        
        if (tab === 'orders') loadOrders();
        if (tab === 'keys') loadKeys();
        if (tab === 'users') loadUsers();
        if (tab === 'coupons') loadCoupons();
        if (tab === 'sale') loadSale();
        if (tab === 'stocklog') loadStockLogs();
        if (tab === 'auditlog') loadAuditLogs();
        if (tab === 'admins') loadAdmins();
        if (tab === 'staff') loadStaff();
        if (tab === 'depositlog') loadWebhookLogs();
    });
});

// ============ NOTICE (공지 보내기) ============
// 이미지 미리보기
document.addEventListener('DOMContentLoaded', function() {
    const imageInput = document.getElementById('notice-image');
    if (imageInput) {
        imageInput.addEventListener('change', function(e) {
            const file = e.target.files[0];
            const preview = document.getElementById('notice-image-preview');
            if (file && preview) {
                const reader = new FileReader();
                reader.onload = function(ev) {
                    preview.src = ev.target.result;
                    preview.style.display = 'block';
                };
                reader.readAsDataURL(file);
            } else if (preview) {
                preview.style.display = 'none';
            }
        });
    }
});

async function sendNotice() {
    const title = document.getElementById('notice-title').value.trim();
    const content = document.getElementById('notice-content').value.trim();
    const color = document.getElementById('notice-color').value.trim();
    const imageInput = document.getElementById('notice-image');
    const resultDiv = document.getElementById('notice-result');
    
    if (!title || !content) {
        showToast('제목과 내용을 입력해주세요.', 'error');
        return;
    }
    
    resultDiv.innerHTML = '<p style="color:#888;">전송 중...</p>';
    try {
        // FormData로 전송 (이미지 파일 지원)
        const formData = new FormData();
        formData.append('title', title);
        formData.append('content', content);
        formData.append('color', color);
        if (imageInput && imageInput.files[0]) {
            formData.append('image', imageInput.files[0]);
        }
        
        const resp = await fetch('/api/notice/send', {
            method: 'POST',
            body: formData
        });
        const data = await resp.json();
        if (data.success) {
            resultDiv.innerHTML = '<p style="color:#2ecc71;font-weight:600;">✅ ' + data.message + '</p>';
            document.getElementById('notice-title').value = '';
            document.getElementById('notice-content').value = '';
            if (imageInput) imageInput.value = '';
            const preview = document.getElementById('notice-image-preview');
            if (preview) preview.style.display = 'none';
        } else {
            resultDiv.innerHTML = '<p style="color:#e74c3c;font-weight:600;">❌ ' + (data.message || data.error || '전송 실패') + '</p>';
        }
    } catch (e) {
        resultDiv.innerHTML = '<p style="color:#e74c3c;font-weight:600;">❌ 공지 전송 실패</p>';
    }
}

// ============ TOAST ============
function showToast(message, type) {
    type = type || 'success';
    const toast = document.getElementById('toast');
    toast.className = 'toast ' + type;
    toast.textContent = message;
    toast.style.display = 'block';
    setTimeout(function() { toast.style.display = 'none'; }, 3000);
}

// ============ MODAL ============
function openModal(id) {
    document.getElementById(id).classList.add('active');
}
function closeModal(id) {
    document.getElementById(id).classList.remove('active');
}

// ============ ORDERS ============
async function loadOrders() {
    const status = document.getElementById('order-filter').value;
    const container = document.getElementById('orders-table-container');
    container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">로딩중...</p>';
    
    try {
        const resp = await fetch('/api/orders' + (status ? '?status=' + status + '&include_keys=true' : '?include_keys=true'));
        const orders = await resp.json();
        
        if (orders.length === 0) {
            container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">주문이 없습니다.</p>';
            return;
        }
        
        const statusBadge = {
            'pending': '<span class="badge badge-pending">대기중</span>',
            'confirmed': '<span class="badge badge-confirmed">확인됨</span>',
            'completed': '<span class="badge badge-completed">완료</span>',
            'cancelled': '<span class="badge badge-cancelled">취소됨</span>'
        };
        
        let html = '<table><thead><tr><th>주문번호</th><th>구매자</th><th>상품</th><th>금액</th><th>상태</th><th>주문시간</th><th>KEY</th><th>작업</th></tr></thead><tbody>';
        orders.forEach(function(o) {
            html += '<tr>';
            html += '<td>#' + o.id + '</td>';
            html += '<td>' + o.username + '</td>';
            html += '<td>' + o.product_name + '</td>';
            html += '<td>' + o.amount.toLocaleString() + '원</td>';
            html += '<td>' + (statusBadge[o.status] || o.status) + '</td>';
            html += '<td>' + o.created_at + '</td>';
            html += '<td>';
            if (o.status === 'completed') {
                if (o.key_value) {
                    html += '<span class="key-value">' + o.key_value + '</span>';
                } else {
                    html += '<button class="btn btn-info btn-sm" onclick="showOrderKey(' + o.id + ')"><i class="fas fa-key"></i> 키 보기</button>';
                }
            } else {
                html += '-';
            }
            html += '</td>';
            html += '<td>';
            if (o.status === 'pending') {
                html += '<button class="btn btn-success btn-sm" onclick="confirmOrder(' + o.id + ')"><i class="fas fa-check"></i> 확인</button> ';
                html += '<button class="btn btn-danger btn-sm" onclick="cancelOrder(' + o.id + ')"><i class="fas fa-times"></i> 취소</button>';
            }
            html += '</td></tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p style="color:#e74c3c;text-align:center;padding:20px;">주문을 불러오는데 실패했습니다.</p>';
    }
}

async function showOrderKey(orderId) {
    try {
        const resp = await fetch('/api/orders/' + orderId + '/key');
        const order = await resp.json();
        if (order && order.key_value) {
            const key = order.key_value;
            if (navigator.clipboard) {
                await navigator.clipboard.writeText(key);
            }
            showToast('🔑 KEY: ' + key + ' (복사됨)', 'info');
        } else {
            showToast('KEY가 없습니다.', 'error');
        }
    } catch (e) {
        showToast('KEY 조회 실패', 'error');
    }
}

async function confirmOrder(orderId) {
    if (!confirm('주문 #' + orderId + '를 확인하시겠습니까?')) return;
    try {
        const resp = await fetch('/api/orders/' + orderId + '/confirm', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            showToast(data.message, 'success');
        } else {
            showToast(data.message, 'error');
        }
        loadOrders();
        setTimeout(function() { location.reload(); }, 1500);
    } catch (e) {
        showToast('주문 확인 실패', 'error');
    }
}

async function cancelOrder(orderId) {
    if (!confirm('주문 #' + orderId + '를 취소하시겠습니까?')) return;
    try {
        const resp = await fetch('/api/orders/' + orderId + '/cancel', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            showToast(data.message, 'success');
        } else {
            showToast(data.message, 'error');
        }
        loadOrders();
        setTimeout(function() { location.reload(); }, 1500);
    } catch (e) {
        showToast('주문 취소 실패', 'error');
    }
}

// ============ PRODUCTS ============
function openProductModal() {
    document.getElementById('product-modal-title').textContent = '상품 추가';
    document.getElementById('product-id').value = '';
    document.getElementById('product-name').value = '';
    document.getElementById('product-price').value = '';
    document.getElementById('product-desc').value = '';
    document.getElementById('product-category').value = '';
    const emojiEl = document.getElementById('product-emoji');
    if (emojiEl) emojiEl.value = '';
    document.getElementById('product-active').value = '1';
    openModal('product-modal');
}

function setProductEmoji(emoji) {
    const emojiEl = document.getElementById('product-emoji');
    if (emojiEl) emojiEl.value = emoji;
}

function editProduct(id) {
    fetch('/api/products').then(function(r) { return r.json(); }).then(function(products) {
        const p = products.find(function(x) { return x.id === id; });
        if (!p) return;
        document.getElementById('product-modal-title').textContent = '상품 수정';
        document.getElementById('product-id').value = p.id;
        document.getElementById('product-name').value = p.name;
        document.getElementById('product-price').value = p.price;
        document.getElementById('product-desc').value = p.description;
        document.getElementById('product-category').value = p.category || '';
        const emojiEl = document.getElementById('product-emoji');
        if (emojiEl) emojiEl.value = p.emoji || '';
        document.getElementById('product-active').value = p.active;
        openModal('product-modal');
    });
}

async function saveProduct() {
    const id = document.getElementById('product-id').value;
    const emojiEl = document.getElementById('product-emoji');
    const data = {
        name: document.getElementById('product-name').value,
        price: parseInt(document.getElementById('product-price').value),
        description: document.getElementById('product-desc').value,
        category: document.getElementById('product-category').value,
        emoji: emojiEl ? emojiEl.value.trim() : '',
        active: parseInt(document.getElementById('product-active').value)
    };
    
    if (!data.name || !data.price) {
        showToast('상품명과 가격을 입력해주세요.', 'error');
        return;
    }
    
    try {
        let resp;
        if (id) {
            resp = await fetch('/api/products/' + id, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
        } else {
            resp = await fetch('/api/products', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
        }
        const result = await resp.json();
        if (result.success) {
            showToast('상품이 저장되었습니다.', 'success');
            closeModal('product-modal');
            setTimeout(function() { location.reload(); }, 1000);
        }
    } catch (e) {
        showToast('상품 저장 실패', 'error');
    }
}

async function deleteProduct(id) {
    if (!confirm('상품을 삭제하시겠습니까? 관련 키도 모두 삭제됩니다.')) return;
    try {
        const resp = await fetch('/api/products/' + id, { method: 'DELETE' });
        const data = await resp.json();
        if (data.success) {
            showToast('상품이 삭제되었습니다.', 'success');
            setTimeout(function() { location.reload(); }, 1000);
        }
    } catch (e) {
        showToast('상품 삭제 실패', 'error');
    }
}

// ============ KEYS ============
function openKeyModal() {
    document.getElementById('key-values').value = '';
    openModal('key-modal');
}

function openKeyModalFor(productId) {
    openKeyModal();
    const select = document.getElementById('key-product');
    if (select) select.value = String(productId);
    const textarea = document.getElementById('key-values');
    if (textarea) textarea.focus();
}

async function saveKeys() {
    const productId = document.getElementById('key-product').value;
    const keys = document.getElementById('key-values').value;
    
    if (!keys.trim()) {
        showToast('키를 입력해주세요.', 'error');
        return;
    }
    
    try {
        const resp = await fetch('/api/products/' + productId + '/keys', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ keys: keys })
        });
        const data = await resp.json();
        if (data.success) {
            showToast(data.count + '개의 키가 추가되었습니다.', 'success');
            closeModal('key-modal');
            loadKeys();
        }
    } catch (e) {
        showToast('키 추가 실패', 'error');
    }
}

async function loadKeys() {
    const productId = document.getElementById('key-product-filter').value;
    const used = document.getElementById('key-used-filter').value;
    const container = document.getElementById('keys-table-container');
    container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">로딩중...</p>';
    
    try {
        let url = '/api/keys?';
        if (productId) url += 'product_id=' + productId + '&';
        if (used !== '') url += 'used=' + used;
        const resp = await fetch(url);
        const keys = await resp.json();
        
        if (keys.length === 0) {
            container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">키가 없습니다.</p>';
            return;
        }
        
        let html = '<table><thead><tr><th>ID</th><th>상품</th><th>KEY</th><th>상태</th><th>작업</th></tr></thead><tbody>';
        keys.forEach(function(k) {
            html += '<tr>';
            html += '<td>' + k.id + '</td>';
            html += '<td>' + k.product_id + '</td>';
            html += '<td><span class="key-value">' + k.key_value + '</span></td>';
            html += '<td>' + (k.used ? '<span class="badge badge-cancelled">사용됨</span>' : '<span class="badge badge-completed">미사용</span>') + '</td>';
            html += '<td><button class="btn btn-danger btn-sm" onclick="deleteKey(' + k.id + ')"><i class="fas fa-trash"></i></button></td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p style="color:#e74c3c;text-align:center;padding:20px;">키를 불러오는데 실패했습니다.</p>';
    }
}

async function deleteKey(id) {
    if (!confirm('키를 삭제하시겠습니까?')) return;
    try {
        const resp = await fetch('/api/keys/' + id, { method: 'DELETE' });
        const data = await resp.json();
        if (data.success) {
            showToast('키가 삭제되었습니다.', 'success');
            loadKeys();
        }
    } catch (e) {
        showToast('키 삭제 실패', 'error');
    }
}

// ============ SETTINGS ============
async function saveSettings() {
    const data = {
        bot_name: document.getElementById('set-bot-name').value,
        discord_token: document.getElementById('set-discord-token').value,
        admin_password: document.getElementById('set-admin-password').value,
        dashboard_url: document.getElementById('set-dashboard-url').value,
        admin_channel_id: document.getElementById('set-admin-channel').value,
        purchase_log_channel_id: document.getElementById('set-purchase-log-channel').value,
        deposit_log_channel_id: document.getElementById('set-deposit-log-channel').value,
        charge_log_channel_id: document.getElementById('set-charge-log-channel').value,
        stock_channel_id: document.getElementById('set-stock-channel').value,
        notice_channel_id: document.getElementById('set-notice-channel').value,
        purchase_role_id: document.getElementById('set-purchase-role').value,
        review_channel_id: document.getElementById('set-review-channel').value,
        use_categories: document.getElementById('set-use-categories').value
    };
    
    // 자판기 이모지 / 출석 포인트
    const vendingEmojiEl = document.getElementById('set-vending-emoji');
    if (vendingEmojiEl) data.vending_emoji = vendingEmojiEl.value;
    const attendancePointsEl = document.getElementById('set-attendance-points');
    if (attendancePointsEl) data.attendance_points = attendancePointsEl.value;

    // 충전 요청 유효 시간 (자동입금 확인)
    const expireEl = document.getElementById('set-charge-expire');
    if (expireEl) data.charge_expire_minutes = expireEl.value;
    
    try {
        const resp = await fetch('/api/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        const result = await resp.json();
        if (result.success) {
            showToast('설정이 저장되었습니다.', 'success');
        }
    } catch (e) {
        showToast('설정 저장 실패', 'error');
    }
}

// ============ DEPOSIT WEBHOOK 진단 (Tasker 자동입금 확인) ============
async function testDepositWebhook() {
    const resultDiv = document.getElementById('webhook-test-result');
    const textEl = document.getElementById('webhook-test-text');
    if (!resultDiv || !textEl) return;
    const text = textEl.value.trim();
    if (!text) {
        resultDiv.innerHTML = '<p style="color:#e74c3c;">알림 텍스트를 입력하세요. (예: 5,000원 입금 홍길동)</p>';
        return;
    }
    resultDiv.innerHTML = '<p style="color:#888;">테스트 중...</p>';
    try {
        const resp = await fetch('/api/deposit-webhook/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ body: text })
        });
        const data = await resp.json();
        if (!data.success) {
            resultDiv.innerHTML = '<p style="color:#e74c3c;">❌ ' + (data.message || '테스트 실패') + '</p>';
            return;
        }
        const p = data.parsed || {};
        let html = '<div style="font-size:13px;font-family:monospace;background:rgba(0,0,0,.25);border-radius:8px;padding:10px;line-height:1.7;">';
        html += '금액 추출: <b style="color:' + (p.amount ? '#2ecc71' : '#e74c3c') + '">' + (p.amount ? p.amount.toLocaleString() + '원' : '실패') + '</b>　|　';
        html += '입금자 추출: <b style="color:' + (p.depositor ? '#2ecc71' : '#e74c3c') + '">' + (p.depositor || '실패') + '</b><br>';
        html += '유효 대기 충전요청: ' + data.pending_count + '개 (유효시간 ' + data.expire_minutes + '분)<br>';
        if (data.matched) {
            html += '<span style="color:#2ecc71;">✅ 매칭 성공: 요청 #' + data.matched.request_id + ' (' + data.matched.username + ' / ' + data.matched.depositor_name + ' / ' + data.matched.amount.toLocaleString() + '원)</span><br>';
        }
        html += '<span style="color:#aaa;">' + (data.match_hint || '') + '</span>';
        html += '</div>';
        resultDiv.innerHTML = html;
    } catch (e) {
        resultDiv.innerHTML = '<p style="color:#e74c3c;">❌ 요청 실패: ' + e + '</p>';
    }
}

// ============ 봇 로그인 진단 (토큰 vs IP 차단 판별) ============
async function checkBotDiag() {
    const box = document.getElementById('bot-diag-result');
    if (!box) return;
    box.innerHTML = '<span style="color:#888;">확인 중... (디스코드 API에 실제 요청)</span>';
    try {
        const resp = await fetch('/api/bot-diag');
        const data = await resp.json();
        const labels = {
            ok: ['#2ecc71', '✅ 토큰 정상'],
            invalid_token: ['#e74c3c', '❌ 토큰 무효 (401)'],
            cloudflare_block: ['#f39c12', '⚠️ 서버 IP 차단 (Cloudflare 1015)'],
            rate_limit: ['#f39c12', '⏳ 요청 과다 (429)'],
            no_token: ['#e74c3c', '❌ 토큰 미설정'],
            network_error: ['#e74c3c', '❌ 네트워크 오류']
        };
        const lv = labels[data.verdict] || ['#f39c12', '⚠️ ' + (data.verdict || '알 수 없음')];
        const t = data.token || {};
        const login = data.login || {};
        let html = '<div style="font-family:monospace;font-size:12px;background:rgba(0,0,0,.25);border-radius:8px;padding:10px;line-height:1.8;">';
        html += '<b style="color:' + lv[0] + ';font-size:13px;">' + lv[1] + '</b>'
            + (data.http_status ? ' <span style="color:#888;">(HTTP ' + data.http_status + ')</span>' : '') + '<br>';
        html += (data.detail || '') + '<br>';
        if (data.bot_name) html += '봇 계정: <b>' + data.bot_name + '</b><br>';
        html += '사용 토큰: <b>' + (data.token_source || '-') + '</b>　|　'
            + '길이 ' + (t.length || 0) + '자　|　지문 <b>' + (t.fingerprint || '-') + '</b><br>';
        html += '형식: ' + (t.looks_like_bot_token ? '<span style="color:#2ecc71;">정상(AAA.BBB.CCC)</span>' : '<span style="color:#f39c12;">비정상 — 토큰 값 확인</span>')
            + '　|　Bot 접두사 ' + (t.has_bot_prefix ? '<span style="color:#e74c3c;">있음(제거 필요)</span>' : '없음')
            + '　|　앞뒤 공백 ' + (t.had_whitespace ? '<span style="color:#e74c3c;">있음</span>' : '없음')
            + '　|　따옴표 ' + (t.had_quotes ? '<span style="color:#e74c3c;">있음</span>' : '없음') + '<br>';
        if (data.tokens_match === true) {
            html += '환경변수 ↔ 대시보드 저장값: <span style="color:#2ecc71;">동일</span><br>';
        } else if (data.tokens_match === false) {
            html += '환경변수 ↔ 대시보드 저장값: <span style="color:#f39c12;">서로 다름</span> — 실제로는 <b>' + data.token_source + '</b>이(가) 쓰입니다<br>';
        }
        html += '마지막 로그인 시도: ' + (login.last_attempt_at || '-')
            + ' (시도 #' + (login.attempts || 0) + ')'
            + '　|　결과: <b>' + (login.last_kind || (login.login_ok_at ? 'ok' : '-')) + '</b><br>';
        if (login.login_ok_at) html += '로그인 성공: ' + login.login_ok_at + ' (' + (login.logged_in_as || '') + ')<br>';
        if (login.last_error) html += '<span style="color:#e74c3c;">최근 오류: ' + String(login.last_error).replace(/</g, '&lt;') + '</span><br>';
        html += '봇 준비 상태(Render 기준): <b>' + (login.bot_ready ? '준비됨' : '아직 미연결') + '</b>';
        html += '</div>';
        box.innerHTML = html;
    } catch (e) {
        box.innerHTML = '<span style="color:#e74c3c;">❌ 진단 요청 실패: ' + e + '</span>';
    }
}

async function loadWebhookLogs() {
    const container = document.getElementById('webhook-logs-container');
    if (!container) return;
    try {
        const resp = await fetch('/api/deposit-webhook/logs');
        const data = await resp.json();
        if (!data.success || !data.logs || !data.logs.length) {
            container.innerHTML = '<div style="color:#888;padding:4px 0;">수신 기록이 없습니다. 폰(Tasker)에서 알림이 전송되지 않았거나 URL/키가 틀렸을 수 있습니다.<br>① 폰에서 웹훅 URL(GET 테스트) 접속 → ② 실제 은행 알림 발생 → ③ 이 로그 새로고침</div>';
            return;
        }
        container.innerHTML = data.logs.map(l => {
            let color = '#aaaaaa';
            if (l.level === 'error') color = '#e74c3c';
            else if (l.level === 'warn') color = '#f39c12';
            else if (l.level === 'match') color = '#2ecc71';
            else if (l.level === 'deposit') color = '#3498db';
            return '<div style="color:' + color + ';padding:2px 0;border-bottom:1px dashed rgba(255,255,255,.06);">[' + l.time + '] ' + l.message.replace(/</g, '&lt;') + '</div>';
        }).join('');
    } catch (e) {
        container.innerHTML = '<div style="color:#e74c3c;">로그 로드 실패: ' + e + '</div>';
    }
}

async function clearWebhookLogs() {
    if (!confirm('웹훅 수신 로그를 모두 삭제하시겠습니까?')) return;
    try {
        const resp = await fetch('/api/deposit-webhook/logs/clear', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            const container = document.getElementById('webhook-logs-container');
            if (container) {
                container.innerHTML = '<div style="color:#888;">로그가 삭제되었습니다.</div>';
            }
            showToast('로그가 삭제되었습니다.', 'success');
        } else {
            showToast('로그 삭제 실패', 'error');
        }
    } catch (e) {
        showToast('로그 삭제 실패', 'error');
    }
}

async function saveBankSettings() {
    const data = {
        bank_name: document.getElementById('set-bank-name').value,
        bank_account: document.getElementById('set-bank-account').value,
        bank_holder: document.getElementById('set-bank-holder').value
    };
    
    try {
        const resp = await fetch('/api/settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        const result = await resp.json();
        if (result.success) {
            showToast('계좌 설정이 저장되었습니다.', 'success');
        }
    } catch (e) {
        showToast('계좌 설정 저장 실패', 'error');
    }
}

// ============ USERS ============
async function loadUsers() {
    const container = document.getElementById('users-container');
    container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">로딩중...</p>';
    
    try {
        const resp = await fetch('/api/users');
        const users = await resp.json();
        
        if (users.length === 0) {
            container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">등록된 유저가 없습니다.</p>';
            return;
        }
        
        let html = '<table><thead><tr><th>유저</th><th>잔액</th><th>가입일</th><th>포인트 지급</th><th>포인트 차감</th></tr></thead><tbody>';
        users.forEach(function(u) {
            html += '<tr>';
            html += '<td><strong>' + u.username + '</strong><br><small style="color:#888;">' + u.user_id + '</small></td>';
            html += '<td><strong style="color:#2ecc71;">' + u.balance.toLocaleString() + '포인트</strong></td>';
            html += '<td>' + u.created_at + '</td>';
            html += '<td>';
            html += '<input type="number" id="points-add-' + u.user_id + '" placeholder="금액" style="width:80px;padding:5px;border:1px solid #ddd;border-radius:5px;margin-right:5px;">';
            html += '<button class="btn btn-success btn-sm" onclick="givePoints(\'' + u.user_id + '\', \'' + u.username + '\')"><i class="fas fa-plus"></i> 지급</button>';
            html += '</td>';
            html += '<td>';
            html += '<input type="number" id="points-deduct-' + u.user_id + '" placeholder="금액" style="width:80px;padding:5px;border:1px solid #ddd;border-radius:5px;margin-right:5px;">';
            html += '<button class="btn btn-danger btn-sm" onclick="deductPoints(\'' + u.user_id + '\')"><i class="fas fa-minus"></i> 차감</button>';
            html += '</td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p style="color:#e74c3c;text-align:center;padding:20px;">유저를 불러오는데 실패했습니다.</p>';
    }
}

async function givePoints(userId, username) {
    const amount = document.getElementById('points-add-' + userId).value;
    if (!amount || parseInt(amount) <= 0) {
        showToast('금액을 입력해주세요.', 'error');
        return;
    }
    
    try {
        const resp = await fetch('/api/users/' + userId + '/points', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ amount: parseInt(amount), username: username })
        });
        const data = await resp.json();
        if (data.success) {
            showToast('포인트가 지급되었습니다! (잔액: ' + data.balance.toLocaleString() + '포인트)', 'success');
            loadUsers();
        } else {
            showToast(data.error || '지급 실패', 'error');
        }
    } catch (e) {
        showToast('포인트 지급 실패', 'error');
    }
}

async function deductPoints(userId) {
    const amount = document.getElementById('points-deduct-' + userId).value;
    if (!amount || parseInt(amount) <= 0) {
        showToast('금액을 입력해주세요.', 'error');
        return;
    }
    
    try {
        const resp = await fetch('/api/users/' + userId + '/points/deduct', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ amount: parseInt(amount) })
        });
        const data = await resp.json();
        if (data.success) {
            showToast('포인트가 차감되었습니다! (잔액: ' + data.balance.toLocaleString() + '포인트)', 'success');
            loadUsers();
        } else {
            showToast(data.error || '차감 실패', 'error');
        }
    } catch (e) {
        showToast('포인트 차감 실패', 'error');
    }
}

// ============ COUPONS (쿠폰 관리) ============
async function loadCoupons() {
    const container = document.getElementById('coupons-container');
    container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">로딩중...</p>';
    
    try {
        const resp = await fetch('/api/coupons');
        const coupons = await resp.json();
        
        if (!Array.isArray(coupons) || coupons.length === 0) {
            container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">등록된 쿠폰이 없습니다.</p>';
            return;
        }
        
        let html = '<table><thead><tr><th>ID</th><th>코드</th><th>할인</th><th>사용 현황</th><th>상태</th><th>생성일</th><th>작업</th></tr></thead><tbody>';
        coupons.forEach(function(c) {
            const uses = c.max_uses > 0 ? c.used_count + '/' + c.max_uses : c.used_count + '/무제한';
            const isPercent = c.discount_type === 'percent';
            const discountText = isPercent
                ? c.discount_amount + '% 할인'
                : c.discount_amount.toLocaleString() + '포인트';
            html += '<tr>';
            html += '<td>' + c.id + '</td>';
            html += '<td><strong>' + c.code + '</strong></td>';
            html += '<td>' + discountText + '</td>';
            html += '<td>' + uses + '</td>';
            html += '<td>' + (c.active ? '<span class="badge badge-completed">활성</span>' : '<span class="badge badge-cancelled">비활성</span>') + '</td>';
            html += '<td>' + c.created_at + '</td>';
            html += '<td>';
            html += '<button class="btn btn-warning btn-sm" onclick="toggleCoupon(' + c.id + ')"><i class="fas fa-toggle-on"></i> ' + (c.active ? '비활성화' : '활성화') + '</button> ';
            html += '<button class="btn btn-danger btn-sm" onclick="deleteCoupon(' + c.id + ')"><i class="fas fa-trash"></i></button>';
            html += '</td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p style="color:#e74c3c;text-align:center;padding:20px;">쿠폰을 불러오는데 실패했습니다.</p>';
    }
}

async function saveCoupon() {
    const code = document.getElementById('coupon-code').value.trim();
    const discount = parseInt(document.getElementById('coupon-discount').value);
    const maxUses = parseInt(document.getElementById('coupon-max-uses').value);
    const typeEl = document.getElementById('coupon-discount-type');
    const discountType = typeEl ? typeEl.value : 'amount';
    
    if (!code) {
        showToast('쿠폰 코드를 입력해주세요.', 'error');
        return;
    }
    if (!discount || discount <= 0) {
        showToast('할인 금액을 입력해주세요.', 'error');
        return;
    }
    if (discountType === 'percent' && discount > 100) {
        showToast('할인율은 100% 이하여야 합니다.', 'error');
        return;
    }
    
    try {
        const resp = await fetch('/api/coupons', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                code: code,
                discount_amount: discount,
                max_uses: isNaN(maxUses) ? 0 : maxUses,
                discount_type: discountType
            })
        });
        const data = await resp.json();
        if (data.success) {
            showToast('쿠폰이 생성되었습니다.', 'success');
            document.getElementById('coupon-code').value = '';
            document.getElementById('coupon-discount').value = '';
            document.getElementById('coupon-max-uses').value = '0';
            loadCoupons();
        } else {
            showToast(data.error || '쿠폰 생성 실패', 'error');
        }
    } catch (e) {
        showToast('쿠폰 생성 실패', 'error');
    }
}

async function deleteCoupon(id) {
    if (!confirm('쿠폰을 삭제하시겠습니까?')) return;
    try {
        const resp = await fetch('/api/coupons/' + id, { method: 'DELETE' });
        const data = await resp.json();
        if (data.success) {
            showToast('쿠폰이 삭제되었습니다.', 'success');
            loadCoupons();
        }
    } catch (e) {
        showToast('쿠폰 삭제 실패', 'error');
    }
}

async function toggleCoupon(id) {
    try {
        const resp = await fetch('/api/coupons/' + id + '/toggle', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            showToast('쿠폰 상태가 변경되었습니다.', 'success');
            loadCoupons();
        }
    } catch (e) {
        showToast('쿠폰 상태 변경 실패', 'error');
    }
}

// ============ ADMINS ============
async function loadAdmins() {
    const container = document.getElementById('admins-container');
    try {
        const resp = await fetch('/api/admins');
        const admins = await resp.json();
        
        if (admins.length === 0) {
            container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">관리자가 없습니다.</p>';
            return;
        }
        
        let html = '<table><thead><tr><th>유저 ID</th><th>이름</th><th>작업</th></tr></thead><tbody>';
        admins.forEach(function(a) {
            html += '<tr>';
            html += '<td>' + a.user_id + '</td>';
            html += '<td>' + a.username + '</td>';
            html += '<td><button class="btn btn-danger btn-sm" onclick="removeAdmin(\'' + a.user_id + '\')"><i class="fas fa-trash"></i> 제거</button></td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p style="color:#e74c3c;text-align:center;padding:20px;">관리자를 불러오는데 실패했습니다.</p>';
    }
}

async function addAdmin() {
    const userId = document.getElementById('new-admin-id').value;
    const username = document.getElementById('new-admin-name').value;
    
    if (!userId) {
        showToast('유저 ID를 입력해주세요.', 'error');
        return;
    }
    
    try {
        const resp = await fetch('/api/admins', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ user_id: userId, username: username })
        });
        const data = await resp.json();
        if (data.success) {
            showToast('관리자가 추가되었습니다.', 'success');
            document.getElementById('new-admin-id').value = '';
            document.getElementById('new-admin-name').value = '';
            loadAdmins();
        }
    } catch (e) {
        showToast('관리자 추가 실패', 'error');
    }
}

async function removeAdmin(userId) {
    if (!confirm('관리자를 제거하시겠습니까?')) return;
    try {
        const resp = await fetch('/api/admins/' + userId, { method: 'DELETE' });
        const data = await resp.json();
        if (data.success) {
            showToast('관리자가 제거되었습니다.', 'success');
            loadAdmins();
        }
    } catch (e) {
        showToast('관리자 제거 실패', 'error');
    }
}

// ============ STAFF (서포터 / 매니저) ============
async function loadStaff() {
    const container = document.getElementById('staff-container');
    if (!container) return;
    try {
        const resp = await fetch('/api/staff');
        const staff = await resp.json();

        if (!staff.length) {
            container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">등록된 서포터가 없습니다.</p>';
            return;
        }

        let html = '<table><thead><tr><th>유저 ID</th><th>이름</th><th>권한</th><th>지정자</th><th>지정일</th><th>작업</th></tr></thead><tbody>';
        staff.forEach(function(s) {
            const isManager = s.role === 'manager';
            const badge = isManager
                ? '<span class="badge badge-completed">🛡️ 매니저</span>'
                : '<span class="badge badge-pending">👤 서포터</span>';
            html += '<tr>';
            html += '<td>' + escapeHtml(s.user_id) + '</td>';
            html += '<td>' + escapeHtml(s.username || '-') + '</td>';
            html += '<td>' + badge + '</td>';
            html += '<td>' + escapeHtml(s.added_by || '-') + '</td>';
            html += '<td>' + escapeHtml(s.created_at || '-') + '</td>';
            html += '<td><button class="btn btn-danger btn-sm" onclick="removeStaff(\'' + escapeHtml(s.user_id) + '\')"><i class="fas fa-trash"></i> 해제</button></td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p style="color:#e74c3c;text-align:center;padding:20px;">서포터 목록을 불러오는데 실패했습니다.</p>';
    }
}

async function addStaff() {
    const userId = document.getElementById('new-staff-id').value.trim();
    const username = document.getElementById('new-staff-name').value.trim();
    const roleEl = document.getElementById('new-staff-role');
    const role = roleEl ? roleEl.value : 'supporter';

    if (!userId) {
        showToast('유저 ID를 입력해주세요.', 'error');
        return;
    }

    try {
        const resp = await fetch('/api/staff', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ user_id: userId, username: username, role: role })
        });
        const data = await resp.json();
        if (data.success) {
            showToast(data.created ? '서포터가 지정되었습니다.' : '서포터 권한이 변경되었습니다.', 'success');
            document.getElementById('new-staff-id').value = '';
            document.getElementById('new-staff-name').value = '';
            loadStaff();
        } else {
            showToast(data.error || '서포터 지정 실패', 'error');
        }
    } catch (e) {
        showToast('서포터 지정 실패', 'error');
    }
}

async function removeStaff(userId) {
    if (!confirm('서포터 권한을 해제하시겠습니까?')) return;
    try {
        const resp = await fetch('/api/staff/' + userId, { method: 'DELETE' });
        const data = await resp.json();
        if (data.success) {
            showToast('서포터 권한이 해제되었습니다.', 'success');
            loadStaff();
        } else {
            showToast(data.error || '서포터 해제 실패', 'error');
        }
    } catch (e) {
        showToast('서포터 해제 실패', 'error');
    }
}

// ============ SALE (전체 할인 이벤트) ============
function escapeHtml(text) {
    return String(text == null ? '' : text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatWon(value) {
    return Number(value || 0).toLocaleString() + '원';
}

async function loadSale() {
    try {
        const resp = await fetch('/api/sale');
        const sale = await resp.json();
        const statusEl = document.getElementById('sale-status');
        if (statusEl) {
            statusEl.innerHTML = sale.active
                ? '<strong style="color:#2ecc71;">🔥 할인 진행중</strong> — ' + escapeHtml(sale.summary || '')
                : '현재 진행 중인 할인이 없습니다.';
        }
        const typeEl = document.getElementById('sale-type');
        const valueEl = document.getElementById('sale-value');
        const labelEl = document.getElementById('sale-label');
        const endsEl = document.getElementById('sale-ends-at');
        if (typeEl) typeEl.value = sale.type || 'percent';
        if (valueEl && sale.value) valueEl.value = sale.value;
        if (labelEl) labelEl.value = sale.label || '';
        if (endsEl) endsEl.value = sale.ends_at || '';
        updateSalePreview();
    } catch (e) {
        showToast('할인 정보를 불러오지 못했습니다.', 'error');
    }
}

function updateSalePreview() {
    const priceEl = document.getElementById('sale-preview-price');
    const valueEl = document.getElementById('sale-value');
    const typeEl = document.getElementById('sale-type');
    const resultEl = document.getElementById('sale-preview-result');
    if (!priceEl || !valueEl || !typeEl || !resultEl) return;

    const price = parseInt(priceEl.value, 10) || 0;
    const value = parseInt(valueEl.value, 10) || 0;
    if (price <= 0 || value <= 0) {
        resultEl.textContent = '할인 값을 입력하면 적용 가격이 표시됩니다.';
        return;
    }
    const discount = typeEl.value === 'percent'
        ? Math.floor(price * Math.min(value, 100) / 100)
        : Math.min(price, value);
    const finalPrice = Math.max(0, price - discount);
    resultEl.innerHTML = '<span style="opacity:.7;text-decoration:line-through;">' + formatWon(price) + '</span>'
        + ' → <strong style="color:#2ecc71;">' + formatWon(finalPrice) + '</strong>'
        + ' <span style="color:#e17055;">(-' + formatWon(discount) + ')</span>';
}

async function saveSale() {
    const payload = {
        type: document.getElementById('sale-type').value,
        value: parseInt(document.getElementById('sale-value').value, 10),
        label: document.getElementById('sale-label').value.trim(),
        ends_at: document.getElementById('sale-ends-at').value.trim(),
        active: true
    };
    if (!payload.value || payload.value <= 0) {
        showToast('할인 값을 입력해주세요.', 'error');
        return;
    }
    try {
        const resp = await fetch('/api/sale', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (data.success) {
            showToast('할인이 적용되었습니다. 자판기가 곧 갱신됩니다.', 'success');
            loadSale();
        } else {
            showToast(data.error || '할인 적용 실패', 'error');
        }
    } catch (e) {
        showToast('할인 적용 실패', 'error');
    }
}

async function stopSale() {
    if (!confirm('전체 할인을 종료하시겠습니까?')) return;
    try {
        const resp = await fetch('/api/sale', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ stop: true })
        });
        const data = await resp.json();
        if (data.success) {
            showToast('할인이 종료되었습니다.', 'success');
            loadSale();
        } else {
            showToast(data.error || '할인 종료 실패', 'error');
        }
    } catch (e) {
        showToast('할인 종료 실패', 'error');
    }
}

// ============ STOCK LOGS (입고 기록) ============
async function loadStockLogs() {
    const container = document.getElementById('stock-logs-container');
    if (!container) return;
    container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">로딩중...</p>';
    try {
        const resp = await fetch('/api/stock-logs?limit=100');
        const logs = await resp.json();
        if (!Array.isArray(logs) || logs.length === 0) {
            container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">입고 기록이 없습니다.</p>';
            return;
        }
        let html = '<table><thead><tr><th>시간</th><th>상품</th><th>수량</th><th>총 재고</th><th>경로</th><th>담당</th></tr></thead><tbody>';
        logs.forEach(function(log) {
            html += '<tr>';
            html += '<td>' + escapeHtml(log.created_at) + '</td>';
            html += '<td>' + escapeHtml(log.product_name) + '</td>';
            html += '<td style="color:#2ecc71;font-weight:700;">+' + log.count + '</td>';
            html += '<td>' + log.total_stock + '</td>';
            html += '<td>' + escapeHtml(log.source) + '</td>';
            html += '<td>' + escapeHtml(log.actor || '-') + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p style="color:#e74c3c;text-align:center;padding:20px;">입고 기록을 불러오지 못했습니다.</p>';
    }
}


// ============ AUDIT LOGS (감사 로그) ============
async function loadAuditLogs() {
    const container = document.getElementById('audit-logs-container');
    if (!container) return;
    container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">로딩중...</p>';
    try {
        const resp = await fetch('/api/audit-logs?limit=200');
        const logs = await resp.json();
        if (!Array.isArray(logs) || logs.length === 0) {
            container.innerHTML = '<p style="color:#888;text-align:center;padding:20px;">감사 로그가 없습니다.</p>';
            return;
        }
        let html = '<table><thead><tr><th>시간</th><th>동작</th><th>대상</th><th>내용</th></tr></thead><tbody>';
        logs.forEach(function(log) {
            html += '<tr>';
            html += '<td>' + escapeHtml(log.created_at) + '</td>';
            html += '<td><strong>' + escapeHtml(log.action) + '</strong></td>';
            html += '<td>' + escapeHtml(log.actor_name || log.actor_id || '-') + '</td>';
            html += '<td>' + escapeHtml(log.detail) + '</td>';
            html += '</tr>';
        });
        html += '</tbody></table>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<p style="color:#e74c3c;text-align:center;padding:20px;">감사 로그를 불러오지 못했습니다.</p>';
    }
}

// ============ DM 전체 공지 ============
async function sendDmNotice() {
    const title = document.getElementById('dm-title').value.trim();
    const content = document.getElementById('dm-content').value.trim();
    const color = document.getElementById('dm-color').value.trim();
    const resultDiv = document.getElementById('dm-result');

    if (!content) {
        showToast('DM 내용을 입력해주세요.', 'error');
        return;
    }
    if (!confirm('모든 유저에게 DM을 보내시겠습니까?')) return;

    resultDiv.innerHTML = '<p style="color:#888;">발송 요청 중...</p>';
    try {
        const resp = await fetch('/api/notice/dm', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ title: title, content: content, color: color })
        });
        const data = await resp.json();
        if (data.success) {
            resultDiv.innerHTML = '<p style="color:#2ecc71;font-weight:600;">✅ ' + escapeHtml(data.message || '전송을 시작했습니다.') + '</p>';
            document.getElementById('dm-content').value = '';
        } else {
            resultDiv.innerHTML = '<p style="color:#e74c3c;font-weight:600;">❌ ' + escapeHtml(data.message || data.error || '전송 실패') + '</p>';
        }
    } catch (e) {
        resultDiv.innerHTML = '<p style="color:#e74c3c;font-weight:600;">❌ 요청 실패: ' + escapeHtml(e) + '</p>';
    }
}

