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
        if (tab === 'admins') loadAdmins();
    });
});

// ============ NOTICE (공지 보내기) ============
async function sendNotice() {
    const title = document.getElementById('notice-title').value.trim();
    const content = document.getElementById('notice-content').value.trim();
    const color = document.getElementById('notice-color').value.trim();
    const resultDiv = document.getElementById('notice-result');
    
    if (!title || !content) {
        showToast('제목과 내용을 입력해주세요.', 'error');
        return;
    }
    
    resultDiv.innerHTML = '<p style="color:#888;">전송 중...</p>';
    try {
        const resp = await fetch('/api/notice/send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ title: title, content: content, color: color })
        });
        const data = await resp.json();
        if (data.success) {
            resultDiv.innerHTML = '<p style="color:#2ecc71;font-weight:600;">✅ ' + data.message + '</p>';
            document.getElementById('notice-title').value = '';
            document.getElementById('notice-content').value = '';
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
    document.getElementById('product-active').value = '1';
    openModal('product-modal');
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
        document.getElementById('product-active').value = p.active;
        openModal('product-modal');
    });
}

async function saveProduct() {
    const id = document.getElementById('product-id').value;
    const data = {
        name: document.getElementById('product-name').value,
        price: parseInt(document.getElementById('product-price').value),
        description: document.getElementById('product-desc').value,
        category: document.getElementById('product-category').value,
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
        pushbullet_token: document.getElementById('set-pushbullet-token').value,
        admin_password: document.getElementById('set-admin-password').value,
        dashboard_url: document.getElementById('set-dashboard-url').value,
        admin_channel_id: document.getElementById('set-admin-channel').value,
        purchase_log_channel_id: document.getElementById('set-purchase-log-channel').value,
        deposit_log_channel_id: document.getElementById('set-deposit-log-channel').value,
        stock_channel_id: document.getElementById('set-stock-channel').value,
        notice_channel_id: document.getElementById('set-notice-channel').value,
        purchase_role_id: document.getElementById('set-purchase-role').value,
        review_channel_id: document.getElementById('set-review-channel').value,
        use_categories: document.getElementById('set-use-categories').value
    };
    
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

async function testPushbullet() {
    const resultDiv = document.getElementById('pushbullet-test-result');
    resultDiv.innerHTML = '<p style="color:#888;">테스트 중...</p>';
    try {
        const resp = await fetch('/api/pushbullet/test', { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            resultDiv.innerHTML = '<p style="color:#2ecc71;font-weight:600;">✅ ' + data.message + '</p>';
        } else {
            resultDiv.innerHTML = '<p style="color:#e74c3c;font-weight:600;">❌ ' + data.message + '</p>';
        }
    } catch (e) {
        resultDiv.innerHTML = '<p style="color:#e74c3c;font-weight:600;">❌ 테스트 실패</p>';
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