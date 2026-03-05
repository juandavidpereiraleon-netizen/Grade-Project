"""
Módulo de base de datos para Metafiance
PostgreSQL (Supabase) con conexiones robustas para producción
"""

import os
import time
import hashlib
from typing import Optional, List, Tuple, Dict
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool


class Database:
    """Clase para manejar todas las operaciones de base de datos de forma segura y robusta"""

    def __init__(self):
        self.db_error = None
        self.pool: Optional[ThreadedConnectionPool] = None
        try:
            self._init_pool()
            # Evitar modificaciones de esquema en producción: solo si explícitamente se permite
            if os.environ.get('DB_INIT', '').lower() in ('1', 'true', 'yes'):
                self.init_database()
        except Exception as e:
            self.db_error = str(e)
            print("Error inicializando conexión a base de datos:", e)

    def _build_dsn(self) -> str:
        # Soporta DATABASE_URL (Render, Supabase) o variables individuales
        dsn = os.environ.get('DATABASE_URL') or os.environ.get('SUPABASE_DB_URL') or ''
        if dsn:
            # Forzar SSL en proveedores gestionados
            if 'sslmode=' not in dsn:
                dsn += ('&' if '?' in dsn or 'postgresql://' in dsn else ' ') + 'sslmode=require'
            return dsn
        # Fallback a variables individuales
        host = os.environ.get('PGHOST', '')
        port = os.environ.get('PGPORT', '5432')
        dbname = os.environ.get('PGDATABASE', 'postgres')
        user = os.environ.get('PGUSER', '')
        password = os.environ.get('PGPASSWORD', '')
        return f"host={host} port={port} dbname={dbname} user={user} password={password} sslmode=require"

    def _init_pool(self):
        dsn = self._build_dsn()
        # Tamaño pequeño y seguro para free tiers
        self.pool = ThreadedConnectionPool(minconn=1, maxconn=int(os.environ.get('DB_MAX_CONN', '5')), dsn=dsn)

    def get_connection(self):
        """Obtiene una conexión del pool con reintentos ante parpadeos."""
        assert self.pool is not None, "Pool de conexiones no inicializado"
        retries = 3
        delay = 0.5
        for i in range(retries):
            try:
                conn = self.pool.getconn()
                # Asegurar autocommit desactivado; manejamos commits explícitos
                conn.autocommit = False
                return conn
            except Exception:
                if i == retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2

    def _put_connection(self, conn):
        if self.pool and conn:
            try:
                self.pool.putconn(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    def _execute(self, sql: str, params: Tuple | List = ()):
        conn = self.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params if params else ())
            conn.commit()
            return True
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._put_connection(conn)

    def _fetchall_dicts(self, sql: str, params: Tuple | List = ()) -> List[Dict]:
        conn = self.get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params if params else ())
                rows = cur.fetchall()
                return rows
        finally:
            self._put_connection(conn)

    def _fetchone_dict(self, sql: str, params: Tuple | List = ()) -> Optional[Dict]:
        rows = self._fetchall_dicts(sql, params)
        return rows[0] if rows else None

    def init_database(self):
        # Operaciones DDL controladas (solo si DB_INIT=true). No se ejecuta por defecto en producción.
        self._execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                nombre_padre TEXT NOT NULL,
                nombre_estudiante TEXT NOT NULL,
                nombre_hija TEXT,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                curso TEXT NOT NULL,
                promocion TEXT NOT NULL,
                es_admin BOOLEAN DEFAULT FALSE,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self._execute("""
            CREATE TABLE IF NOT EXISTS metas (
                id SERIAL PRIMARY KEY,
                nombre TEXT NOT NULL,
                curso TEXT NOT NULL,
                fecha_limite TEXT NOT NULL,
                costo_estimado REAL NOT NULL,
                fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self._execute("""
            CREATE TABLE IF NOT EXISTS usuario_metas (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL,
                meta_id INTEGER NOT NULL,
                UNIQUE(usuario_id, meta_id),
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE,
                FOREIGN KEY (meta_id) REFERENCES metas(id) ON DELETE CASCADE
            )
        """)

        self._execute("""
            CREATE TABLE IF NOT EXISTS movimientos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL,
                meta_id INTEGER NOT NULL,
                tipo TEXT NOT NULL CHECK(tipo IN ('ahorro','salida')),
                monto REAL NOT NULL,
                descripcion TEXT,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE,
                FOREIGN KEY (meta_id) REFERENCES metas(id) ON DELETE CASCADE
            )
        """)

        self._execute("""
            CREATE TABLE IF NOT EXISTS ahorros (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL,
                meta_id INTEGER NOT NULL,
                monto REAL NOT NULL,
                descripcion TEXT,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self._execute("""
            CREATE TABLE IF NOT EXISTS salidas (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER NOT NULL,
                meta_id INTEGER NOT NULL,
                monto REAL NOT NULL,
                descripcion TEXT,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    @staticmethod
    def hash_password(password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    def registrar_usuario(self, nombre_padre: str, nombre_estudiante: str,
                          email: str, password: str, curso: str, promocion: str) -> bool:
        try:
            hashed = self.hash_password(password)
            self._execute("""
                INSERT INTO usuarios (nombre_padre, nombre_estudiante, email, password, curso, promocion)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (nombre_padre, nombre_estudiante, email, hashed, curso, promocion))
            return True
        except Exception:
            return False

    def autenticar_usuario(self, email: str, password: str) -> Optional[Dict]:
        hashed = self.hash_password(password)
        row = self._fetchone_dict("""
            SELECT id, nombre_padre, nombre_estudiante, email, curso, promocion, es_admin
            FROM usuarios WHERE email = %s AND password = %s
        """, (email, hashed))
        if row:
            return {
                'id': row['id'],
                'nombre_padre': row['nombre_padre'],
                'nombre_estudiante': row['nombre_estudiante'],
                'email': row['email'],
                'curso': row['curso'],
                'promocion': row['promocion'],
                'es_admin': bool(row['es_admin'])
            }
        return None

    def obtener_usuario(self, usuario_id: int) -> Optional[Dict]:
        row = self._fetchone_dict("""
            SELECT id, nombre_padre, nombre_estudiante, email, curso, promocion, es_admin
            FROM usuarios WHERE id = %s
        """, (usuario_id,))
        if row:
            return {
                'id': row['id'],
                'nombre_padre': row['nombre_padre'],
                'nombre_estudiante': row['nombre_estudiante'],
                'email': row['email'],
                'curso': row['curso'],
                'promocion': row['promocion'],
                'es_admin': bool(row['es_admin'])
            }
        return None

    def crear_meta(self, nombre: str, curso: str, fecha_limite: str, costo_estimado: float) -> int:
        conn = self.get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                INSERT INTO metas (nombre, curso, fecha_limite, costo_estimado)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (nombre, curso, fecha_limite, costo_estimado))
            row = cur.fetchone()
            conn.commit()
            return row['id'] if row else 0
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._put_connection(conn)

    def obtener_metas(self) -> List[Dict]:
        return self._fetchall_dicts("""
            SELECT id, nombre, curso, fecha_limite, costo_estimado, fecha_creacion
            FROM metas ORDER BY fecha_creacion DESC
        """)

    def obtener_meta(self, meta_id: int) -> Optional[Dict]:
        return self._fetchone_dict("""
            SELECT id, nombre, curso, fecha_limite, costo_estimado, fecha_creacion
            FROM metas WHERE id = %s
        """, (meta_id,))

    def actualizar_meta(self, meta_id: int, nombre: str, curso: str,
                        fecha_limite: str, costo_estimado: float) -> bool:
        try:
            self._execute("""
                UPDATE metas
                SET nombre = %s, curso = %s, fecha_limite = %s, costo_estimado = %s
                WHERE id = %s
            """, (nombre, curso, fecha_limite, costo_estimado, meta_id))
            return True
        except Exception:
            return False

    def eliminar_meta(self, meta_id: int) -> bool:
        try:
            self._execute("DELETE FROM metas WHERE id = %s", (meta_id,))
            return True
        except Exception:
            return False

    def asignar_meta_usuario(self, usuario_id: int, meta_id: int) -> bool:
        try:
            self._execute("""
                INSERT INTO usuario_metas (usuario_id, meta_id)
                VALUES (%s, %s)
                ON CONFLICT (usuario_id, meta_id) DO NOTHING
            """, (usuario_id, meta_id))
            return True
        except Exception:
            return False

    def obtener_metas_usuario(self, usuario_id: int) -> List[Dict]:
        return self._fetchall_dicts("""
            SELECT DISTINCT m.id, m.nombre, m.curso, m.fecha_limite, m.costo_estimado, m.fecha_creacion
            FROM metas m
            LEFT JOIN usuario_metas um ON m.id = um.meta_id
            WHERE um.usuario_id = %s OR m.curso = (SELECT curso FROM usuarios WHERE id = %s)
            ORDER BY m.fecha_creacion DESC
        """, (usuario_id, usuario_id))

    def registrar_movimiento(self, usuario_id: int, meta_id: int,
                             tipo: str, monto: float, descripcion: str = "") -> bool:
        try:
            self._execute("""
                INSERT INTO movimientos (usuario_id, meta_id, tipo, monto, descripcion)
                VALUES (%s, %s, %s, %s, %s)
            """, (usuario_id, meta_id, tipo, monto, descripcion))
            if tipo == 'ahorro':
                self._execute("""
                    INSERT INTO ahorros (usuario_id, meta_id, monto, descripcion)
                    VALUES (%s, %s, %s, %s)
                """, (usuario_id, meta_id, monto, descripcion))
            elif tipo == 'salida':
                self._execute("""
                    INSERT INTO salidas (usuario_id, meta_id, monto, descripcion)
                    VALUES (%s, %s, %s, %s)
                """, (usuario_id, meta_id, monto, descripcion))
            return True
        except Exception:
            return False

    def obtener_movimientos_meta(self, usuario_id: int, meta_id: int) -> List[Dict]:
        return self._fetchall_dicts("""
            SELECT id, tipo, monto, descripcion, fecha
            FROM movimientos
            WHERE usuario_id = %s AND meta_id = %s
            ORDER BY fecha DESC
        """, (usuario_id, meta_id))

    def calcular_balance_meta(self, usuario_id: int, meta_id: int) -> Dict:
        ah = self._fetchone_dict("""
            SELECT COALESCE(SUM(monto), 0) AS total FROM ahorros
            WHERE usuario_id = %s AND meta_id = %s
        """, (usuario_id, meta_id))
        ahorrado = ah['total'] if ah else 0
        sa = self._fetchone_dict("""
            SELECT COALESCE(SUM(monto), 0) AS total FROM salidas
            WHERE usuario_id = %s AND meta_id = %s
        """, (usuario_id, meta_id))
        salidas = sa['total'] if sa else 0
        cr = self._fetchone_dict("SELECT costo_estimado FROM metas WHERE id = %s", (meta_id,))
        costo_estimado = cr['costo_estimado'] if cr else 0
        balance = ahorrado - salidas
        faltante = max(0, costo_estimado - balance)
        return {
            'ahorrado': ahorrado,
            'salidas': salidas,
            'balance': balance,
            'costo_estimado': costo_estimado,
            'faltante': faltante
        }

    def listar_usuarios(self) -> List[Dict]:
        return self._fetchall_dicts("""
            SELECT id, nombre_padre, nombre_estudiante, nombre_hija, email, curso, promocion, es_admin, fecha_registro
            FROM usuarios ORDER BY fecha_registro DESC
        """)

    # ===== Métodos adicionales usados por app.py (no destructivos por defecto) =====
    def listar_registros(self) -> List[Dict]:
        """Registros de movimientos para vista de administración"""
        return self._fetchall_dicts("""
            SELECT m.id, m.usuario_id, m.meta_id, m.tipo, m.monto, m.descripcion, m.fecha,
                   u.nombre_estudiante, u.email, me.nombre AS meta_nombre
            FROM movimientos m
            LEFT JOIN usuarios u ON u.id = m.usuario_id
            LEFT JOIN metas me ON me.id = m.meta_id
            ORDER BY m.fecha DESC
            LIMIT 200
        """)

    def wipe_except_admin(self) -> None:
        """Elimina datos de usuarios NO administradores (solo bajo invocación explícita)."""
        self._execute("""
            DELETE FROM ahorros WHERE usuario_id IN (SELECT id FROM usuarios WHERE es_admin = FALSE);
        """)
        self._execute("""
            DELETE FROM salidas WHERE usuario_id IN (SELECT id FROM usuarios WHERE es_admin = FALSE);
        """)
        self._execute("""
            DELETE FROM movimientos WHERE usuario_id IN (SELECT id FROM usuarios WHERE es_admin = FALSE);
        """)
        self._execute("""
            DELETE FROM usuario_metas WHERE usuario_id IN (SELECT id FROM usuarios WHERE es_admin = FALSE);
        """)
        self._execute("""
            DELETE FROM usuarios WHERE es_admin = FALSE;
        """)


if __name__ == "__main__":
    # Modo utilidad local: no realizar escrituras destructivas por defecto
    db = Database()
    print("Conexión a base de datos inicializada correctamente")
