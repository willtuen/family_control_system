import pymysql
import bcrypt
import yaml
import os


DB_NAME = 'family_time_control'


def column_exists(cursor, table_name, column_name):
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        LIMIT 1
        """,
        (table_name, column_name)
    )
    return cursor.fetchone() is not None


def ensure_column(cursor, table_name, column_name, column_definition):
    if column_exists(cursor, table_name, column_name):
        return
    cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")

def init_db():
    config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    mysql_cfg = config['mysql']
    
    # 创建数据库
    conn = pymysql.connect(
        host=mysql_cfg['host'],
        port=mysql_cfg['port'],
        user=mysql_cfg['user'],
        password=mysql_cfg['password'],
        charset='utf8mb4'
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    finally:
        conn.close()
    
    # 连接目标库
    mysql_cfg_full = mysql_cfg.copy()
    mysql_cfg_full['database'] = DB_NAME
    conn = pymysql.connect(**mysql_cfg_full)
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS kids (
                id VARCHAR(50) PRIMARY KEY,
                password VARCHAR(255) NOT NULL,
                balance_seconds INT DEFAULT 3600,
                daily_limit_seconds INT DEFAULT 3600,
                used_today_seconds INT DEFAULT 0,
                usage_date DATE NULL,
                is_active BOOLEAN DEFAULT FALSE,
                active_since DATETIME NULL,
                last_heartbeat DATETIME NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            ensure_column(cursor, 'kids', 'balance_seconds', 'INT DEFAULT 3600')
            ensure_column(cursor, 'kids', 'daily_limit_seconds', 'INT DEFAULT 3600')
            ensure_column(cursor, 'kids', 'used_today_seconds', 'INT DEFAULT 0')
            ensure_column(cursor, 'kids', 'usage_date', 'DATE NULL')
            ensure_column(cursor, 'kids', 'is_active', 'BOOLEAN DEFAULT FALSE')
            ensure_column(cursor, 'kids', 'active_since', 'DATETIME NULL')
            ensure_column(cursor, 'kids', 'last_heartbeat', 'DATETIME NULL')
            ensure_column(cursor, 'kids', 'last_session_end', 'DATETIME NULL')
            ensure_column(cursor, 'kids', 'max_session_duration', 'INT DEFAULT 1800')
            ensure_column(cursor, 'kids', 'cooldown_duration', 'INT DEFAULT 1800')
            ensure_column(cursor, 'kids', 'temp_entry_used_date', 'DATE NULL')
            ensure_column(cursor, 'kids', 'temp_entry_remaining_seconds', 'INT DEFAULT 0')

            cursor.execute("""
                UPDATE kids
                SET daily_limit_seconds = 3600
                WHERE daily_limit_seconds IS NULL OR daily_limit_seconds <= 0
            """)
            cursor.execute("""
                UPDATE kids
                SET used_today_seconds = 0
                WHERE used_today_seconds IS NULL
            """)

            # 创建 CDK 表
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS cdk_codes (
                code VARCHAR(50) PRIMARY KEY,
                seconds INT NOT NULL,
                is_used BOOLEAN DEFAULT FALSE,
                used_by VARCHAR(50) NULL,
                used_at DATETIME NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # 插入一些测试 CDK
            test_cdk_list = [
                ("TEST001", 600),   # 10分钟
                ("TEST002", 1800),  # 30分钟
                ("TEST003", 3600),  # 60分钟
            ]
            for code, seconds in test_cdk_list:
                cursor.execute("""
                    INSERT INTO cdk_codes (code, seconds)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE code = code
                """, (code, seconds))

            default_kids = [("kid01", "123456"), ("kid02", "123456"), ("kid03", "123456")]
            for kid_id, plain_pwd in default_kids:
                hashed = bcrypt.hashpw(plain_pwd.encode(), bcrypt.gensalt()).decode()
                cursor.execute("""
                    INSERT INTO kids (id, password)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE id = id
                """, (kid_id, hashed))

            # 创建admin管理员账户（无密码限制，在配置文件中验证）
            cursor.execute("""
                INSERT INTO kids (id, password, balance_seconds, daily_limit_seconds)
                VALUES ('admin', '', 999999999, 999999999)
                ON DUPLICATE KEY UPDATE id = id
            """)

            conn.commit()
            print("数据库初始化成功（可重复运行）！默认账号：kid01/kid02/kid03，密码：123456")
            print("管理员账户：admin，密码在 config.yaml 的 admin_password 中配置")
            print("测试CDK：TEST001(10分钟), TEST002(30分钟), TEST003(60分钟)")
    finally:
        conn.close()

if __name__ == '__main__':
    init_db()