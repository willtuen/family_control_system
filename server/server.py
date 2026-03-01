from flask import Flask, request, jsonify
import pymysql
import bcrypt
import yaml
import os
from datetime import datetime

app = Flask(__name__)

# 加载配置
config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
with open(config_path) as f:
    CONFIG = yaml.safe_load(f)

def get_db():
    mysql_cfg = CONFIG['mysql'].copy()
    mysql_cfg['database'] = 'family_time_control'
    mysql_cfg['cursorclass'] = pymysql.cursors.DictCursor
    return pymysql.connect(**mysql_cfg)




def split_elapsed_seconds(last_heartbeat, now):
    if not last_heartbeat:
        return 0, 0

    total_elapsed = int((now - last_heartbeat).total_seconds())
    if total_elapsed <= 0:
        return 0, 0

    if last_heartbeat.date() == now.date():
        return 0, total_elapsed

    start_of_today = datetime.combine(now.date(), datetime.min.time())
    today_elapsed = int((now - start_of_today).total_seconds())
    today_elapsed = max(0, min(total_elapsed, today_elapsed))
    previous_day_elapsed = total_elapsed - today_elapsed
    return previous_day_elapsed, today_elapsed


def check_cooldown(kid, now):
    """检查用户是否在冷却期"""
    last_session_end = kid.get('last_session_end')
    cooldown_duration = int(kid.get('cooldown_duration') or 0)

    if not last_session_end or cooldown_duration <= 0:
        return False, 0

    elapsed_since_end = int((now - last_session_end).total_seconds())
    cooldown_remaining = max(0, cooldown_duration - elapsed_since_end)

    return cooldown_remaining > 0, cooldown_remaining


def settle_usage(cursor, kid, now):
    kid_id = kid['id']
    balance = int(kid.get('balance_seconds') or 0)
    daily_limit = int(kid.get('daily_limit_seconds') or 0)
    used_today = int(kid.get('used_today_seconds') or 0)
    usage_date = kid.get('usage_date')
    today = now.date()

    if usage_date != today:
        used_today = 0

    prev_elapsed, today_elapsed = split_elapsed_seconds(kid.get('last_heartbeat'), now)
    total_elapsed = prev_elapsed + today_elapsed

    consumed_prev = min(prev_elapsed, balance)
    balance_after_prev = balance - consumed_prev

    daily_remaining = max(0, daily_limit - used_today)
    consumed_today = min(today_elapsed, balance_after_prev, daily_remaining)

    consumed_total = consumed_prev + consumed_today
    new_balance = balance - consumed_total
    new_used_today = used_today + consumed_today

    hit_limit_during_interval = consumed_total < total_elapsed
    daily_limit_reached = new_used_today >= daily_limit
    balance_exhausted = new_balance <= 0
    should_stop = hit_limit_during_interval or daily_limit_reached or balance_exhausted

    cursor.execute(
        """
        UPDATE kids
        SET balance_seconds = %s,
            used_today_seconds = %s,
            daily_limit_seconds = %s,
            usage_date = %s,
            last_heartbeat = %s
        WHERE id = %s
        """,
        (
            new_balance,
            new_used_today,
            daily_limit,
            today,
            now,
            kid_id,
        )
    )

    return {
        "balance_seconds": new_balance,
        "used_today_seconds": new_used_today,
        "daily_limit_seconds": daily_limit,
        "should_stop": should_stop,
    }

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    kid_id = data.get('kid_id')
    pwd = data.get('password')

    if not kid_id or not pwd:
        return jsonify({"error": "缺少 kid_id 或 password"}), 400

    # Admin账户特殊处理：直接验证配置中的密码
    if kid_id == 'admin':
        admin_pwd = CONFIG.get('security', {}).get('admin_password', 'admin888')
        if pwd == admin_pwd:
            conn = get_db()
            cursor = conn.cursor()
            now = datetime.utcnow()
            # 检查是否已在其他地方登录
            cursor.execute("SELECT is_active FROM kids WHERE id = %s", ('admin',))
            result = cursor.fetchone()
            if result and result.get('is_active'):
                cursor.close()
                conn.close()
                return jsonify({"error": "管理员已在其他设备使用中"}), 403

            # 直接更新admin为活跃状态
            cursor.execute("""
                UPDATE kids
                SET is_active = TRUE, active_since = %s, last_heartbeat = %s
                WHERE id = %s
            """, (now, now, 'admin'))
            conn.commit()
            cursor.close()
            conn.close()

            return jsonify({
                "status": "ok",
                "is_admin": True,
                "balance_seconds": 999999999,
                "daily_remaining_seconds": 999999999
            })
        else:
            return jsonify({"error": "管理员密码错误"}), 401

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM kids WHERE id = %s", (kid_id,))
            kid = cursor.fetchone()

            if not kid:
                return jsonify({"error": "账号不存在"}), 401

            hashed_password = kid['password']
            if isinstance(hashed_password, str):
                hashed_password = hashed_password.encode()

            if not bcrypt.checkpw(pwd.encode(), hashed_password):
                return jsonify({"error": "密码错误"}), 401

            now = datetime.utcnow()

            # 检查冷却状态
            is_cooldown, cooldown_remaining = check_cooldown(kid, now)
            if is_cooldown:
                return jsonify({
                    "error": "账号冷却中",
                    "cooldown_remaining_seconds": cooldown_remaining
                }), 403

            if kid['balance_seconds'] <= 0:
                return jsonify({"error": "时间已用完"}), 403

            if kid['is_active']:
                return jsonify({"error": "已在其他设备使用中"}), 403

            today = now.date()
            used_today = int(kid.get('used_today_seconds') or 0)
            daily_limit = int(kid.get('daily_limit_seconds') or 0)

            if kid.get('usage_date') != today:
                used_today = 0
                cursor.execute(
                    "UPDATE kids SET usage_date = %s, used_today_seconds = 0 WHERE id = %s",
                    (today, kid_id)
                )

            if used_today >= daily_limit:
                conn.commit()
                return jsonify({"error": "今日时长已用完"}), 403

            cursor.execute("""
                UPDATE kids
                SET is_active = TRUE, active_since = %s, last_heartbeat = %s, usage_date = %s
                WHERE id = %s
            """, (now, now, today, kid_id))
            conn.commit()

            return jsonify({
                "status": "ok",
                "balance_seconds": kid['balance_seconds'],
                "daily_remaining_seconds": max(0, daily_limit - used_today)
            })
    finally:
        conn.close()

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    kid_id = data.get('kid_id')

    # Admin账户特殊处理：跳过所有时间检查
    if kid_id == 'admin':
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                now = datetime.utcnow()
                # 只更新心跳时间，不做任何检查
                cursor.execute("""
                    UPDATE kids SET last_heartbeat = %s
                    WHERE id = 'admin' AND is_active = TRUE
                """, (now,))
                conn.commit()
                return jsonify({
                    "action": "continue",
                    "is_admin": True,
                    "balance_seconds": 999999999,
                    "daily_remaining_seconds": 999999999
                })
        finally:
            conn.close()

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM kids WHERE id = %s", (kid_id,))
            kid = cursor.fetchone()

            if not kid or not kid['is_active']:
                return jsonify({"action": "logout"}), 403

            now = datetime.utcnow()

            # 检查是否是临时进入状态
            temp_remaining = int(kid.get('temp_entry_remaining_seconds') or 0)
            is_temp_entry = temp_remaining > 0

            if is_temp_entry:
                # 临时进入模式：计算临时进入剩余时间
                if kid.get('active_since'):
                    session_duration = int((now - kid['active_since']).total_seconds())
                    temp_remaining = max(0, temp_remaining - session_duration)

                    # 更新数据库中的临时进入剩余时间
                    cursor.execute("""
                        UPDATE kids SET temp_entry_remaining_seconds = %s, last_heartbeat = %s
                        WHERE id = %s
                    """, (temp_remaining, now, kid_id))

                    if temp_remaining <= 0:
                        # 临时进入时间用完，退出
                        cursor.execute("""
                            UPDATE kids SET is_active = FALSE, temp_entry_remaining_seconds = 0
                            WHERE id = %s
                        """, (kid_id,))
                        conn.commit()
                        return jsonify({
                            "action": "temp_entry_expired",
                            "message": "临时进入时间已用完"
                        })

                conn.commit()
                return jsonify({
                    "action": "continue",
                    "temp_entry": True,
                    "temp_remaining_seconds": temp_remaining,
                    "balance_seconds": kid['balance_seconds'],
                    "daily_remaining_seconds": max(0, int(kid.get('daily_limit_seconds') or 0) - int(kid.get('used_today_seconds') or 0))
                })
            else:
                # 正常模式
                usage_result = settle_usage(cursor, kid, now)

                # 检查单次使用时长
                max_session = int(kid.get('max_session_duration') or 0)
                if max_session > 0 and kid.get('active_since'):
                    session_duration = int((now - kid['active_since']).total_seconds())
                    if session_duration >= max_session:
                        cursor.execute("""
                            UPDATE kids SET is_active = FALSE, last_session_end = %s
                            WHERE id = %s
                        """, (now, kid_id))
                        conn.commit()
                        return jsonify({
                            "action": "session_limit",
                            "balance_seconds": usage_result['balance_seconds'],
                            "daily_remaining_seconds": max(0, usage_result['daily_limit_seconds'] - usage_result['used_today_seconds']),
                            "cooldown_duration_seconds": int(kid.get('cooldown_duration') or 0)
                        })

                if usage_result['should_stop']:
                    cursor.execute("UPDATE kids SET is_active = FALSE WHERE id = %s", (kid_id,))
                    conn.commit()
                    return jsonify({
                        "action": "time_up",
                        "balance_seconds": usage_result['balance_seconds'],
                        "daily_remaining_seconds": max(0, usage_result['daily_limit_seconds'] - usage_result['used_today_seconds'])
                    })

                conn.commit()
                return jsonify({
                    "action": "continue",
                    "balance_seconds": usage_result['balance_seconds'],
                    "daily_remaining_seconds": max(0, usage_result['daily_limit_seconds'] - usage_result['used_today_seconds'])
                })
    finally:
        conn.close()

@app.route('/api/logout', methods=['POST'])
def logout():
    data = request.json
    kid_id = data.get('kid_id')

    # Admin账户特殊处理：直接注销，不做结算
    if kid_id == 'admin':
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE kids SET is_active = FALSE, active_since = NULL
                    WHERE id = 'admin'
                """)
                conn.commit()
                return jsonify({"status": "logged_out"})
        finally:
            conn.close()

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM kids WHERE id = %s AND is_active = TRUE", (kid_id,))
            kid = cursor.fetchone()

            if kid:
                now = datetime.utcnow()
                usage_result = settle_usage(cursor, kid, now)

                cursor.execute("""
                    UPDATE kids
                    SET is_active = FALSE,
                        active_since = NULL,
                        balance_seconds = %s,
                        used_today_seconds = %s,
                        daily_limit_seconds = %s,
                        usage_date = %s,
                        last_heartbeat = %s,
                        last_session_end = %s
                    WHERE id = %s
                """, (
                    usage_result['balance_seconds'],
                    usage_result['used_today_seconds'],
                    usage_result['daily_limit_seconds'],
                    now.date(),
                    now,
                    now,  # last_session_end
                    kid_id
                ))
                conn.commit()
            
            return jsonify({"status": "logged_out"})
    finally:
        conn.close()

@app.route('/api/reward', methods=['POST'])
def reward():
    data = request.json
    token = data.get('token')
    if token != CONFIG['security']['admin_token']:
        return jsonify({"error": "无效 token"}), 403
        
    kid_id = data.get('kid_id')
    minutes = data.get('minutes', 0)
    
    if not isinstance(minutes, int) or minutes <= 0:
        return jsonify({"error": "minutes 必须是正整数"}), 400
        
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            add_seconds = minutes * 60
            cursor.execute("""
                UPDATE kids
                SET balance_seconds = balance_seconds + %s
                WHERE id = %s
            """, (add_seconds, kid_id))
            if cursor.rowcount == 0:
                return jsonify({"error": "kid_id 不存在"}), 404
            conn.commit()
            return jsonify({"status": "ok"})
    finally:
        conn.close()

@app.route('/api/cdk/redeem', methods=['POST'])
def cdk_redeem():
    data = request.json
    kid_id = data.get('kid_id')
    cdk_code = data.get('cdk_code')

    if not kid_id or not cdk_code:
        return jsonify({"error": "缺少 kid_id 或 cdk_code"}), 400

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 检查 CDK 是否存在且未使用
            cursor.execute("SELECT * FROM cdk_codes WHERE code = %s", (cdk_code,))
            cdk = cursor.fetchone()

            if not cdk:
                return jsonify({"error": "CDK 不存在"}), 404

            if cdk['is_used']:
                return jsonify({"error": "CDK 已被使用"}), 403

            # 获取用户信息
            cursor.execute("SELECT * FROM kids WHERE id = %s", (kid_id,))
            kid = cursor.fetchone()

            if not kid:
                return jsonify({"error": "账号不存在"}), 404

            now = datetime.utcnow()

            # 兑换 CDK：增加余额，标记 CDK 为已使用
            cursor.execute("""
                UPDATE kids
                SET balance_seconds = balance_seconds + %s
                WHERE id = %s
            """, (cdk['seconds'], kid_id))

            cursor.execute("""
                UPDATE cdk_codes
                SET is_used = TRUE, used_by = %s, used_at = %s
                WHERE code = %s
            """, (kid_id, now, cdk_code))

            conn.commit()

            return jsonify({
                "status": "ok",
                "added_seconds": cdk['seconds'],
                "new_balance_seconds": kid['balance_seconds'] + cdk['seconds']
            })
    finally:
        conn.close()

@app.route('/api/temp_entry', methods=['POST'])
def temp_entry():
    """临时进入系统（每天一次，5分钟）"""
    data = request.json
    kid_id = data.get('kid_id')
    pwd = data.get('password')

    if not kid_id or not pwd:
        return jsonify({"error": "缺少 kid_id 或 password"}), 400

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM kids WHERE id = %s", (kid_id,))
            kid = cursor.fetchone()

            if not kid:
                return jsonify({"error": "账号不存在"}), 401

            hashed_password = kid['password']
            if isinstance(hashed_password, str):
                hashed_password = hashed_password.encode()

            if not bcrypt.checkpw(pwd.encode(), hashed_password):
                return jsonify({"error": "密码错误"}), 401

            now = datetime.utcnow()
            today = now.date()

            # 检查今天是否已使用临时进入
            temp_used_date = kid.get('temp_entry_used_date')
            if temp_used_date == today:
                return jsonify({"error": "今天已使用过临时进入功能"}), 403

            # 如果已在使用中，拒绝
            if kid['is_active']:
                return jsonify({"error": "账号已在其他设备使用中"}), 403

            # 临时进入：5分钟 = 300秒
            temp_seconds = 300

            cursor.execute("""
                UPDATE kids
                SET is_active = TRUE,
                    active_since = %s,
                    last_heartbeat = %s,
                    temp_entry_used_date = %s,
                    temp_entry_remaining_seconds = %s,
                    usage_date = %s,
                    used_today_seconds = 0
                WHERE id = %s
            """, (now, now, today, temp_seconds, today, kid_id))
            conn.commit()

            return jsonify({
                "status": "ok",
                "temp_entry": True,
                "temp_remaining_seconds": temp_seconds
            })
    finally:
        conn.close()

@app.route('/api/force_reset', methods=['POST'])
def force_reset():
    """强制重置用户登录状态（用于异常退出后的恢复）"""
    data = request.json
    kid_id = data.get('kid_id')
    password = data.get('password')

    if not kid_id or not password:
        return jsonify({"error": "缺少 kid_id 或 password"}), 400

    # Admin账户特殊处理：直接验证配置中的密码
    if kid_id == 'admin':
        admin_pwd = CONFIG.get('security', {}).get('admin_password', 'admin888')
        if password != admin_pwd:
            return jsonify({"error": "管理员密码错误"}), 401
    else:
        conn = get_db()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM kids WHERE id = %s", (kid_id,))
                kid = cursor.fetchone()

                if not kid:
                    return jsonify({"error": "账号不存在"}), 401

                hashed_password = kid['password']
                if isinstance(hashed_password, str):
                    hashed_password = hashed_password.encode()

                if not bcrypt.checkpw(password.encode(), hashed_password):
                    return jsonify({"error": "密码错误"}), 401
        finally:
            conn.close()

    conn = get_db()
    try:
        with conn.cursor() as cursor:
            # 重置登录状态，保留余额和今日使用数据
            now = datetime.utcnow()
            cursor.execute("""
                UPDATE kids
                SET is_active = FALSE,
                    active_since = NULL,
                    last_heartbeat = NULL
                WHERE id = %s
            """, (kid_id,))
            conn.commit()

            return jsonify({
                "status": "ok",
                "message": "登录状态已重置"
            })
    finally:
        conn.close()

if __name__ == '__main__':
    app.run(
        host=CONFIG['server']['host'],
        port=CONFIG['server']['port'],
        debug=False
    )