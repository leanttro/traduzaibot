# --- [IMPORTAÇÕES] ---
# Flask e WebSockets
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from dotenv import load_dotenv

# Banco de Dados (do seu app Oceano)
import psycopg2
import psycopg2.extras
import traceback
import decimal

# Autenticação (do seu app Oceano)
import jwt
from functools import wraps
import string
import random
from datetime import datetime, timedelta

# IA e Google Search (do seu app Oceano/Taurus)
import os
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- 1. CONFIGURAÇÃO INICIAL ---
print("ℹ️  Iniciando o TraduzAIBot v2 (DB+Auth+Socket)...")
load_dotenv()
app = Flask(__name__, template_folder='.', static_folder='.', static_url_path='')
# Permite todas as origens (CORS) para HTTP e WebSockets
socketio = SocketIO(app, cors_allowed_origins="*")

# --- 2. CONFIGURAÇÃO DAS CHAVES (App Oceano) ---
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default-fallback-secret-key-12345')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
CUSTOM_SEARCH_API_KEY = os.getenv('CUSTOM_SEARCH_API_KEY')
CUSTOM_SEARCH_CX_ID = os.getenv('CUSTOM_SEARCH_CX_ID')

# --- 3. CONEXÃO COM BANCO DE DADOS (App Oceano) ---
def get_db_connection():
    """Cria e retorna uma conexão com o banco de dados PostgreSQL."""
    try:
        db_url = os.getenv('DATABASE_URL')
        if not db_url:
            raise ValueError("DATABASE_URL não configurada")
        conn = psycopg2.connect(db_url)
        return conn
    except Exception as e:
        print(f"❌ ERRO CRÍTICO: Não foi possível conectar ao banco de dados: {e}")
        raise

# Helper para gerar código (App Oceano)
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
        
        # Ferramenta de busca do Google (do seu TaurusBot)
        def tool_google_search(query):
            """Ferramenta: Busca no Google. Use para notícias, fatos ou qualquer pergunta
            sobre o mundo que não seja uma tradução."""
            print(f"🛠️  Executando Google Search para: {query}")
            try:
                service = build("customsearch", "v1", developerKey=CUSTOM_SEARCH_API_KEY)
                res = service.cse().list(q=query, cx=CUSTOM_SEARCH_CX_ID, num=3).execute()
                snippets = [item['snippet'] for item in res.get('items', [])]
                if not snippets:
                    return {"error": "Nenhum resultado encontrado."}
                return {"results": snippets}
            except HttpError as e:
                print(f"❌ Erro na API do Google Search: {e}")
                return {"error": f"Erro na API de busca: {e}"}
            except Exception as e:
                print(f"❌ Erro inesperado na busca: {e}")
                return {"error": "Erro desconhecido ao processar a busca."}

        # Declaração da ferramenta para o Gemini
        tools_for_gemini = [
            {
                "function_declarations": [
                    {
                        "name": "tool_google_search",
                        "description": "Busca informações em tempo real no Google.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "query": {"type": "STRING", "description": "A pergunta ou termo a ser buscado."}
                            },
                            "required": ["query"]
                        }
                    }
                ]
            }
        ]
        
        # 
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
    """Serve a página principal do chat (index.html)."""
    return render_template('index.html')

@app.route('/api/auth/register', methods=['POST'])
def register_user():
    """Registra um novo usuário na tabela traduzaibot_users."""
    data = request.get_json()
    username = data.get('username')
    email = data.get('email')

    if not username or not email:
        return jsonify({'error': 'Username e Email são obrigatórios.'}), 400

    access_code = generate_access_code()
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO traduzaibot_users (username, email, access_code) VALUES (%s, %s, %s)",
            (username, email, access_code)
        )
        conn.commit()
        cur.close()
        # Retorna o código para o usuário anotar
        return jsonify({'message': 'Registro bem-sucedido!', 'access_code': access_code}), 201
    except psycopg2.IntegrityError as e:
        conn.rollback()
        if 'traduzaibot_users_email_key' in str(e):
            return jsonify({'error': 'Este email já está cadastrado.'}), 409
        return jsonify({'error': 'Erro de integridade no banco de dados.'}), 500
    except Exception as e:
        if conn: conn.rollback()
        print(f"❌ Erro no registro: {e}")
        return jsonify({'error': 'Erro interno no servidor.'}), 500
    finally:
        if conn: conn.close()

@app.route('/api/auth/login', methods=['POST'])
def login_user():
    """Valida o código de acesso e retorna um JWT."""
    data = request.get_json()
    access_code = data.get('access_code')
    if not access_code:
        return jsonify({'error': 'Código de Acesso é obrigatório.'}), 400
    
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT id, username, email FROM traduzaibot_users WHERE access_code = %s", (access_code,))
        user = cur.fetchone()
        cur.close()
        
        if user:
            # Cria o Token JWT (do seu app Oceano)
            token = jwt.encode({
                'user_id': user['id'],
                'username': user['username'],
                'email': user['email'],
                'exp': datetime.utcnow() + timedelta(days=7) # Token dura 7 dias
            }, app.config['SECRET_KEY'], algorithm="HS256")
            
            return jsonify({
                'message': 'Login bem-sucedido!', 
                'token': token,
                'user': {'id': user['id'], 'username': user['username'], 'email': user['email']}
            })
        else:
            return jsonify({'error': 'Código de Acesso inválido.'}), 401
    except Exception as e:
        print(f"❌ Erro no login: {e}")
        return jsonify({'error': 'Erro interno no servidor.'}), 500
    finally:
        if conn: conn.close()

# --- 6. HANDLERS DE CHAT (WEBSOCKET) ---
# 

# Dicionário para mapear ID do usuário -> ID do Socket
# Essencial para saber para quem enviar mensagens privadas (na Fase 2)
user_socket_map = {}

def get_user_from_token(token):
    """Helper para decodificar um token JWT e retornar os dados do usuário."""
    if not token:
        return None
    try:
        data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
        return data
    except Exception as e:
        print(f"⚠️  Token inválido: {e}")
        return None

@socketio.on('connect')
def handle_connect():
    """Chamado quando um novo usuário se conecta (mas ainda não está autenticado)."""
    print(f"🔌 Cliente (socket) conectado: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    """Chamado quando um usuário se desconecta."""
    print(f"🔌 Cliente (socket) desconectado: {request.sid}")
    # Remove o usuário do nosso mapa de sockets
    user_id_to_remove = None
    for uid, sid in user_socket_map.items():
        if sid == request.sid:
            user_id_to_remove = uid
            break
    if user_id_to_remove:
        del user_socket_map[user_id_to_remove]
        print(f"👤 Usuário {user_id_to_remove} removido do mapa de sockets.")

@socketio.on('authenticate')
def handle_authentication(data):
    """Recebe o token JWT do cliente para autenticar o socket."""
    token = data.get('token')
    user_data = get_user_from_token(token)
    
    if user_data:
        user_id = user_data.get('user_id')
        username = user_data.get('username')
        
        # Mapeia o user_id ao socket_id
        user_socket_map[user_id] = request.sid
        
        print(f"✅ Usuário '{username}' (ID: {user_id}) autenticado no socket {request.sid}")
        emit('auth_success', {'message': f'Bem-vindo, {username}!'})
    else:
        print(f"❌ Falha na autenticação do socket {request.sid}")
        emit('auth_error', {'error': 'Token inválido ou expirado. Faça login novamente.'})

@socketio.on('send_message')
def handle_chat_message(data):
    """
    Recebe uma mensagem, traduz, salva no DB e transmite.
    Esta é a versão FASE 1.5 (Broadcast Global).
    """
    token = data.get('token')
    user_data = get_user_from_token(token)
    
    # 1. VERIFICA AUTENTICAÇÃO
    if not user_data or not gemini_model:
        emit('chat_error', {'error': 'Autenticação falhou ou IA não está pronta.'}, room=request.sid)
        return

    user_id = user_data.get('user_id')
    username = user_data.get('username')
    
    user_message = data.get('message', '')
    my_lang = data.get('my_lang', 'Português')
    target_lang = data.get('target_lang', 'Inglês')
    
    # 2. INTERCEPTADOR DE AJUDA (Fase 3 do plano)
    if user_message.lower().startswith('/ajuda'):
        query = user_message.lower().replace('/ajuda', '').strip()
        print(f"🤖 Interceptado /ajuda: '{query}'")
        
        try:
            # Inicia o chat com o Gemini (com ferramentas)
            chat = gemini_model.start_chat()
            # Envia a mensagem forçando o uso da ferramenta
            response = chat.send_message(
                f"Use a ferramenta de busca para responder esta pergunta no idioma '{my_lang}': {query}",
            )
            
            # Executa a chamada da ferramenta (se houver)
            function_call = response.candidates[0].content.parts[0].function_call
            if function_call.name == "tool_google_search":
                tool_response = tool_google_search(function_call.args['query'])
                
                # Envia o resultado da ferramenta de volta para o Gemini
                response = chat.send_message(
                    part=genai.Part(function_response=genai.FunctionResponse(
                        name=function_call.name,
                        response=tool_response
                    ))
                )
            
            help_text = response.candidates[0].content.parts[0].text
            
            # Prepara o pacote de ajuda (parece uma mensagem do "Sistema")
            message_packet = {
                'username': 'Sistema de Ajuda',
                'original_message': user_message,
                'original_lang': my_lang,
                'translated_message': help_text,
                'translated_lang': my_lang,
                'timestamp': datetime.now().strftime('%H:%M')
            }
            # Emite a ajuda SOMENTE de volta para o usuário que perguntou
            emit('receive_message', message_packet, room=request.sid)
            return # Para a execução aqui

        except Exception as e:
            print(f"❌ Erro no /ajuda: {e}")
            emit('chat_error', {'error': f'Erro ao processar /ajuda: {e}'}, room=request.sid)
            return

    # 3. LÓGICA DE TRADUÇÃO (Normal)
    print(f"💬 Mensagem de '{username}': '{user_message}' (Traduzir de {my_lang} para {target_lang})")
    translation_prompt = f"""
    Traduza o texto a seguir, do idioma '{my_lang}' para o idioma '{target_lang}'.
    Responda APENAS com o texto traduzido. Não adicione saudações ou explicações.
    Texto: "{user_message}"
    """
    
    try:
        # Chama o Gemini (sem ferramentas, só tradução)
        response = genai.GenerativeModel('gemini-2.5-flash-preview-09-2025').generate_content(
            translation_prompt,
            safety_settings={'HATE': 'BLOCK_NONE', 'HARASSMENT': 'BLOCK_NONE'}
        )
        translated_text = response.text.strip()
        print(f"🤖 Tradução: '{translated_text}'")

        # 4. SALVA NO BANCO DE DADOS (tabela 'traduzaibot_messages')
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            # room_id=1 é a nossa "sala global" por padrão
            sql_insert_msg = """
            INSERT INTO traduzaibot_messages 
            (room_id, sender_id, message_original, message_translated, original_lang, translated_lang)
            VALUES (%s, %s, %s, %s, %s, %s);
            """
            cur.execute(sql_insert_msg, (1, user_id, user_message, translated_text, my_lang, target_lang))
            conn.commit()
            cur.close()
        except Exception as db_e:
            if conn: conn.rollback()
            print(f"❌ Erro ao salvar mensagem no DB: {db_e}")
        finally:
            if conn: conn.close()

        # 5. Prepara o pacote de dados para enviar a TODOS
        message_packet = {
            'username': username, # Agora é o nome real do DB
            'original_message': user_message,
            'original_lang': my_lang,
            'translated_message': translated_text,
            'translated_lang': target_lang,
            'timestamp': datetime.now().strftime('%H:%M')
        }
        
        # Emite para TODOS (broadcast=True) - Fase 1.5
        emit('receive_message', message_packet, broadcast=True)

    except Exception as e:
        print(f"❌ Erro ao chamar a API do Gemini (Tradução): {e}")
        emit('chat_error', {'error': f'Erro na IA: {e}'}, room=request.sid)


# --- 7. Execução do Servidor ---
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"🚀 Servidor Socket.IO (v2) rodando em http://localhost:{port}")
    # Usa socketio.run()
    try:
        socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)
    except ImportError:
        print("⚠️  'gevent' não encontrado. Rodando em modo de polling padrão.")
        socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)