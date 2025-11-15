# --- [IMPORTAÇÕES] ---
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
import traceback
import jwt
from functools import wraps
import string
import random
from datetime import datetime, timedelta
import os
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- 1. CONFIGURAÇÃO INICIAL ---
print("ℹ️  Iniciando o TraduzAIBot v3 (Chat Privado por Salas)...")
load_dotenv()
app = Flask(__name__, template_folder='.', static_folder='.', static_url_path='')
# REMOVIDO: async_mode='gevent' da inicialização para evitar conflitos com Gunicorn/Render
socketio = SocketIO(app, cors_allowed_origins="*")

# --- 2. CONFIGURAÇÃO DAS CHAVES ---
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default-fallback-secret-key-12345')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
CUSTOM_SEARCH_API_KEY = os.getenv('CUSTOM_SEARCH_API_KEY')
CUSTOM_SEARCH_CX_ID = os.getenv('CUSTOM_SEARCH_CX_ID')

# --- 3. CONEXÃO COM BANCO DE DADOS ---
def get_db_connection():
    try:
        db_url = os.getenv('DATABASE_URL')
        if not db_url:
            raise ValueError("DATABASE_URL não configurada")
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        print(f"❌ ERRO CRÍTICO: Não foi possível conectar ao banco de dados: {e}")
        raise

# Helper para gerar código
def generate_access_code(length=8):
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choice(characters) for i in range(length))

# --- 4. CONFIGURAÇÃO DO GEMINI (COM FERRAMENTAS) ---
gemini_model = None
if not GEMINI_API_KEY:
    print("❌ ERRO CRÍTICO: GEMINI_API_KEY não encontrada.")
else:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        
        def tool_google_search(query):
            """Ferramenta: Busca no Google."""
            print(f"🛠️  Executando Google Search para: {query}")
            try:
                service = build("customsearch", "v1", developerKey=CUSTOM_SEARCH_API_KEY)
                res = service.cse().list(q=query, cx=CUSTOM_SEARCH_CX_ID, num=3).execute()
                snippets = [item['snippet'] for item in res.get('items', [])]
                return {"results": snippets} if snippets else {"error": "Nenhum resultado."}
            except Exception as e:
                return {"error": f"Erro na API de busca: {e}"}

        tools_for_gemini = [{"function_declarations": [{"name": "tool_google_search", "description": "Busca informações em tempo real no Google.", "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}, "required": ["query"]}}]}]
        
        gemini_model = genai.GenerativeModel(
            model_name='gemini-2.5-flash-preview-09-2025',
            tools=tools_for_gemini
        )
        print("✅  Modelo Gemini ('gemini-2.5-flash-preview-09-2025') e Google Search inicializados.")
    except Exception as e:
        print(f"❌ Erro ao inicializar o modelo Gemini: {e}")

# --- 5. ROTAS DE AUTENTICAÇÃO (HTTP) ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/auth/register', methods=['POST'])
def register_user():
    data = request.get_json()
    # CORREÇÃO: Usando .strip() para evitar espaços em branco
    username = data.get('username').strip() if data.get('username') else None
    email = data.get('email').strip() if data.get('email') else None
    
    if not username or not email:
        return jsonify({'error': 'Username e Email são obrigatórios.'}), 400
    
    access_code = generate_access_code()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO traduzaibot_users (username, email, access_code) VALUES (%s, %s, %s)",
            # CORREÇÃO: Forçando o código de acesso a ser salvo em letras maiúsculas
            (username, email, access_code.upper())
        )
        conn.commit()
        # Retorna o código em maiúsculas
        return jsonify({'message': 'Registro bem-sucedido!', 'access_code': access_code.upper()}), 201
    except psycopg2.IntegrityError:
        conn.rollback()
        return jsonify({'error': 'Este email já está cadastrado.'}), 409
    except Exception as e:
        if conn: conn.rollback()
        return jsonify({'error': f'Erro interno: {e}'}), 500
    finally:
        if conn: conn.close()

@app.route('/api/auth/login', methods=['POST'])
def login_user():
    data = request.get_json()
    # CORREÇÃO CRÍTICA: Remove espaços e coloca em MAIÚSCULAS para bater com o DB
    access_code = data.get('access_code', '').strip().upper() 
    
    if not access_code:
        return jsonify({'error': 'Código de Acesso é obrigatório.'}), 400
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        # Busca com o código MAIÚSCULO e sem espaços
        cur.execute("SELECT id, username, email FROM traduzaibot_users WHERE access_code = %s", (access_code,))
        user = cur.fetchone()
        if user:
            token = jwt.encode({
                'user_id': user['id'],
                'username': user['username'],
                'email': user['email'],
                'exp': datetime.utcnow() + timedelta(days=7)
            }, app.config['SECRET_KEY'], algorithm="HS256")
            return jsonify({
                'message': 'Login bem-sucedido!', 
                'token': token,
                'user': {'id': user['id'], 'username': user['username'], 'email': user['email']}
            })
        else:
            # ERRO 401: Código inválido
            return jsonify({'error': 'Código de Acesso inválido.'}), 401
    except Exception as e:
        return jsonify({'error': f'Erro interno: {e}'}), 500
    finally:
        if conn: conn.close()

# --- 6. ROTAS PROTEGIDAS (HTTP) ---

def token_required(f):
    """Decorador para proteger rotas que exigem um token JWT."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            token = request.headers['Authorization'].split(" ")[1]
        if not token:
            return jsonify({'error': 'Token é obrigatório!'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            # Passa o user_id para a rota
            return f(data['user_id'], *args, **kwargs)
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expirou!'}), 401
        except Exception:
            return jsonify({'error': 'Token é inválido!'}), 401
    return decorated

@app.route('/api/chat/find_user', methods=['POST'])
@token_required
def find_user(current_user_id):
    """Busca um usuário pelo email ou código para iniciar chat."""
    data = request.get_json()
    # CORREÇÃO: Remove espaços e coloca em MAIÚSCULAS
    query = data.get('query', '').strip().upper() 
    if not query:
        return jsonify({'error': 'Termo de busca (email ou código) é obrigatório.'}), 400
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        # Busca por email OU código, e que NÃO SEJA o próprio usuário
        cur.execute(
            # A busca por email não deve ser .upper(), apenas o código
            "SELECT id, username, email FROM traduzaibot_users WHERE (email = %s OR access_code = %s) AND id != %s",
            (query.lower(), query, current_user_id) # Usando lower() para email e upper() para código
        )
        user = cur.fetchone()
        if user:
            return jsonify({'id': user['id'], 'username': user['username'], 'email': user['email']})
        else:
            return jsonify({'error': 'Usuário não encontrado ou é você mesmo.'}), 404
    except Exception as e:
        print(f"❌ Erro ao buscar usuário: {e}")
        return jsonify({'error': f'Erro interno: {e}'}), 500
    finally:
        if conn: conn.close()

@app.route('/api/chat/users', methods=['GET'])
@token_required
def list_users(current_user_id):
    """Lista todos os usuários exceto o usuário logado (para o Lobby)."""
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        # Busca todos os usuários exceto o logado
        cur.execute("SELECT id, username, email FROM traduzaibot_users WHERE id != %s ORDER BY username ASC", (current_user_id,))
        users = cur.fetchall()
        
        # Busca as conversas existentes do usuário logado
        cur.execute("""
            SELECT p2.user_id as partner_id, t.username as partner_username, r.id as room_id
            FROM traduzaibot_room_participants p1
            JOIN traduzaibot_room_participants p2 ON p1.room_id = p2.room_id AND p1.user_id != p2.user_id
            JOIN traduzaibot_users t ON p2.user_id = t.id
            JOIN traduzaibot_chat_rooms r ON p1.room_id = r.id
            WHERE p1.user_id = %s
        """, (current_user_id,))
        conversations = cur.fetchall()

        return jsonify({'users': users, 'conversations': conversations})
    except Exception as e:
        print(f"❌ Erro ao listar usuários: {e}")
        return jsonify({'error': f'Erro interno: {e}'}), 500
    finally:
        if conn: conn.close()

# --- 7. HANDLERS DE CHAT (WEBSOCKET) ---
# (Restante do SocketIO omitido por ser igual)
user_socket_map = {} 
socket_user_map = {} 

def get_user_from_token(token):
    if not token: return None
    try:
        return jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
    except:
        return None

@socketio.on('connect')
def handle_connect():
    print(f"🔌 Cliente (socket) conectado: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    print(f"🔌 Cliente (socket) desconectado: {request.sid}")
    user_id = socket_user_map.pop(request.sid, None)
    if user_id:
        user_socket_map.pop(user_id, None)
        print(f"👤 Usuário {user_id} desconectado e removido dos mapas.")

@socketio.on('authenticate')
def handle_authentication(data):
    """Autentica o socket e coloca o usuário em todas as suas salas de chat existentes."""
    token = data.get('token')
    user_data = get_user_from_token(token)
    
    if not user_data:
        emit('auth_error', {'error': 'Token inválido ou expirado.'})
        return

    user_id = user_data['user_id']
    username = user_data['username']
    
    # Mapeia user_id <-> socket.sid
    user_socket_map[user_id] = request.sid
    socket_user_map[request.sid] = user_id
    
    print(f"✅ Usuário '{username}' (ID: {user_id}) autenticado no socket {request.sid}")
    emit('auth_success', {'message': f'Bem-vindo, {username}!'})

    # (A MÁGICA) Coloca o usuário em todas as salas de chat que ele já participa
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        # Busca todas as salas que este usuário participa
        cur.execute("SELECT room_id FROM traduzaibot_room_participants WHERE user_id = %s", (user_id,))
        rooms = cur.fetchall()
        
        room_list = []
        for room in rooms:
            room_id_str = str(room['room_id'])
            join_room(room_id_str) # Coloca o socket na sala
            room_list.append(room_id_str)
            
        print(f"🚪 Usuário {user_id} adicionado às salas: {room_list}")
        
    except Exception as e:
        print(f"❌ Erro ao buscar/entrar em salas: {e}")
    finally:
        if conn: conn.close()

@socketio.on('request_conversation')
def handle_request_conversation(data):
    """Inicia uma nova conversa (ou encontra uma existente) com outro usuário."""
    token = data.get('token')
    user_data = get_user_from_token(token)
    if not user_data: return

    my_user_id = user_data['user_id']
    my_username = user_data['username']
    target_user_id = data.get('target_user_id')

    if not target_user_id:
        emit('chat_error', {'error': 'ID do usuário alvo é obrigatório.'})
        return
    
    user_ids = sorted([my_user_id, target_user_id])
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # 1. Tenta encontrar uma sala privada existente entre esses dois usuários
        cur.execute("""
            SELECT p1.room_id
            FROM traduzaibot_room_participants p1
            JOIN traduzaibot_room_participants p2 ON p1.room_id = p2.room_id
            JOIN traduzaibot_chat_rooms r ON p1.room_id = r.id
            WHERE p1.user_id = %s AND p2.user_id = %s AND r.is_private = TRUE
        """, (user_ids[0], user_ids[1]))
        
        existing_room = cur.fetchone()
        
        if existing_room:
            room_id = existing_room['room_id']
            # Emite a sala de volta para o solicitante
            emit('conversation_ready', {'room_id': room_id}, room=request.sid)
        else:
            # 2. Cria uma nova sala
            cur.execute("INSERT INTO traduzaibot_chat_rooms (is_private) VALUES (TRUE) RETURNING id")
            new_room_id = cur.fetchone()['id']
            
            # Adiciona os dois participantes
            cur.execute(
                "INSERT INTO traduzaibot_room_participants (user_id, room_id) VALUES (%s, %s), (%s, %s)",
                (user_ids[0], new_room_id, user_ids[1], new_room_id)
            )
            conn.commit()
            
            # 3. Coloca o solicitante (User A) na sala do Socket.IO
            join_room(str(new_room_id))
            
            # 4. Envia o convite para o User B (se ele estiver online)
            target_socket_sid = user_socket_map.get(target_user_id)
            if target_socket_sid:
                # Coloca o User B na sala do Socket.IO também
                join_room(str(new_room_id), sid=target_socket_sid)
                
                # [CORREÇÃO] Busca o nome do usuário alvo para o convite
                cur.execute("SELECT username FROM traduzaibot_users WHERE id = %s", (target_user_id,))
                target_user_name = cur.fetchone()['username']
                
                # Emite o "convite" (que é apenas a sala nova) para o User B
                emit('new_conversation_invite', {
                    'room_id': new_room_id,
                    'with_user': {'id': my_user_id, 'username': my_username, 'partner_name': target_user_name}
                }, room=target_socket_sid)

            # 5. Emite a sala de volta para o solicitante (User A)
            emit('conversation_ready', {'room_id': new_room_id}, room=request.sid)
            
    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ Erro ao criar/buscar sala: {e}")
        emit('chat_error', {'error': f'Erro ao iniciar conversa: {e}'})
    finally:
        if conn: conn.close()

@socketio.on('request_chat_history')
def handle_chat_history(data):
    """Busca o histórico de mensagens de uma sala específica."""
    token = data.get('token')
    user_data = get_user_from_token(token)
    if not user_data: return

    room_id = data.get('room_id')
    if not room_id: return

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        
        # Busca mensagens E o nome do remetente
        cur.execute("""
            SELECT m.*, u.username 
            FROM traduzaibot_messages m
            JOIN traduzaibot_users u ON m.sender_id = u.id
            WHERE m.room_id = %s
            ORDER BY m.timestamp ASC
        """, (room_id,))
        
        messages = cur.fetchall()
        
        # Converte para JSON serializável
        message_list = []
        for msg in messages:
            message_list.append({
                'id': msg['id'],
                'room_id': msg['room_id'],
                'sender_id': msg['sender_id'],
                'username': msg['username'],
                'message_original': msg['message_original'],
                'message_translated': msg['message_translated'],
                'original_lang': msg['original_lang'],
                'translated_lang': msg['translated_lang'],
                'timestamp': msg['timestamp'].strftime('%Y-%m-%dT%H:%M:%S')
            })
        
        # Envia o histórico SÓ para o solicitante
        emit('chat_history_loaded', {'room_id': room_id, 'messages': message_list}, room=request.sid)
        
    except Exception as e:
        print(f"❌ Erro ao buscar histórico: {e}")
        emit('chat_error', {'error': f'Erro ao buscar histórico: {e}'})
    finally:
        if conn: conn.close()
        
@socketio.on('send_message')
def handle_chat_message(data):
    """Recebe uma mensagem, traduz, salva no DB e transmite PARA A SALA."""
    token = data.get('token')
    user_data = get_user_from_token(token)
    if not user_data or not gemini_model:
        emit('chat_error', {'error': 'Autenticação falhou ou IA não está pronta.'})
        return

    user_id = user_data['user_id']
    username = user_data['username']
    
    room_id = data.get('room_id')
    user_message = data.get('message', '')
    my_lang = data.get('my_lang', 'Português')
    target_lang = data.get('target_lang', 'Inglês')
    
    if not room_id:
        emit('chat_error', {'error': 'ID da Sala é obrigatório.'})
        return

    # 2. INTERCEPTADOR DE AJUDA
    if user_message.lower().startswith('/ajuda'):
        query = user_message.lower().replace('/ajuda', '').strip()
        print(f"🤖 Interceptado /ajuda: '{query}'")
        try:
            chat = gemini_model.start_chat()
            response = chat.send_message(
                f"Use a ferramenta de busca para responder esta pergunta no idioma '{my_lang}': {query}",
            )
            function_call = response.candidates[0].content.parts[0].function_call
            if function_call.name == "tool_google_search":
                tool_response = tool_google_search(function_call.args['query'])
                response = chat.send_message(
                    part=genai.Part(function_response=genai.FunctionResponse(
                        name=function_call.name,
                        response=tool_response
                    ))
                )
            help_text = response.candidates[0].content.parts[0].text
            message_packet = {
                'room_id': room_id,
                'username': 'Sistema de Ajuda',
                'original_message': user_message, 'original_lang': my_lang,
                'translated_message': help_text, 'translated_lang': my_lang,
                'timestamp': datetime.now().strftime('%H:%M')
            }
            emit('receive_message', message_packet, room=request.sid)
            return
        except Exception as e:
            print(f"❌ Erro no /ajuda: {e}")
            emit('chat_error', {'error': f'Erro ao processar /ajuda: {e}'})
            return

    # 3. LÓGICA DE TRADUÇÃO (Normal)
    print(f"💬 Sala {room_id} | Mensagem de '{username}': '{user_message}'")
    translation_prompt = f"""
    Traduza o texto a seguir, do idioma '{my_lang}' para o idioma '{target_lang}'.
    Responda APENAS com o texto traduzido.
    Texto: "{user_message}"
    """
    
    try:
        response = genai.GenerativeModel('gemini-2.5-flash-preview-09-2025').generate_content(
            translation_prompt,
            safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE'}
        )
        translated_text = response.text.strip()

        # 4. SALVA NO BANCO DE DADOS (com o room_id correto)
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            sql_insert_msg = """
            INSERT INTO traduzaibot_messages 
            (room_id, sender_id, message_original, message_translated, original_lang, translated_lang)
            VALUES (%s, %s, %s, %s, %s, %s);
            """
            cur.execute(sql_insert_msg, (room_id, user_id, user_message, translated_text, my_lang, target_lang))
            conn.commit()
        except Exception as db_e:
            if conn: conn.rollback()
            print(f"❌ Erro ao salvar mensagem no DB: {db_e}")
        finally:
            if conn: conn.close()

        # 5. Prepara o pacote de dados
        message_packet = {
            'room_id': room_id, 
            'username': username,
            'original_message': user_message,
            'original_lang': my_lang,
            'translated_message': translated_text,
            'translated_lang': target_lang,
            'timestamp': datetime.now().strftime('%H:%M')
        }
        
        # 6. Emite para a SALA ESPECÍFICA (corrigido: usa str(room_id))
        emit('receive_message', message_packet, room=str(room_id))

    except Exception as e:
        print(f"❌ Erro ao chamar a API do Gemini (Tradução): {e}")
        emit('chat_error', {'error': f'Erro na IA: {e}'})

# --- 8. Execução do Servidor ---
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor Socket.IO (v3 - Salas) rodando em http://localhost:{port}")
    # Nota: Removido 'async_mode' para maior compatibilidade com Gunicorn no Render
    try:
        socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)
    except ImportError:
        socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)