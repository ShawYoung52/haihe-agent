// 等待 DOM 加载
window.addEventListener('load', function() {
    // 创建自定义头部
    const header = document.createElement('div');
    header.className = 'custom-header';
    header.innerHTML = `
        <div class="header-left">
            <div class="logo-icon">司南</div>
            <div class="brand-text">
                <span class="brand-title">海河流域数字预报员</span>
                <span class="brand-subtitle">Haihe River Basin Digital Forecaster</span>
            </div>
        </div>
        <div class="header-right" id="userMenu">
            <div class="user-avatar">TJ</div>
            <span class="user-name">JORDAN DOE</span>
            <span class="dropdown-icon">▼</span>
        </div>
    `;

    document.body.prepend(header);

    // 用户菜单点击事件
    document.getElementById('userMenu').addEventListener('click', function() {
        // 触发 Chainlit 的用户操作或自定义下拉菜单
        console.log('打开用户菜单');
    });
});