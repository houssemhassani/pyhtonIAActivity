# ia-server/app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import numpy as np

app = Flask(__name__)
CORS(app)

def calculate_productivity_score(data):
    """
    Calcule le score de productivité (0-100)
    """
    score = 100
    
    # 1. Pénalité pour captures identiques (pas de travail réel)
    if data['screenshots']['total'] > 0:
        identical_rate = data['screenshots']['identical'] / data['screenshots']['total']
        score -= identical_rate * 30
    
    # 2. Bonus pour activité élevée
    if data['activity']['avg_activity_level'] > 80:
        score += 10
    elif data['activity']['avg_activity_level'] < 50:
        score -= 15
    
    # 3. Pénalité pour heures manquantes
    expected_hours = 8 * data['attendance']['days_present']
    if data['attendance']['total_work_hours'] < expected_hours:
        missing_hours = expected_hours - data['attendance']['total_work_hours']
        score -= (missing_hours / expected_hours) * 25
    
    # 4. Bonus pour tâches complétées
    score += data['tasks']['completion_rate'] * 0.15
    
    # 5. Pénalité pour retards
    if data['attendance']['days_late'] > 0:
        score -= data['attendance']['days_late'] * 5
    
    # 6. Bonus pour clics (activité)
    if data['activity']['keyboard_clicks'] > 5000:
        score += 5
    if data['activity']['mouse_clicks'] > 3000:
        score += 5
    
    return max(0, min(100, int(score)))

def generate_recommendations(data):
    recommendations = []
    
    if data['activity']['avg_activity_level'] < 60:
        recommendations.append("📈 Augmentez votre activité régulière sur l'ordinateur")
    
    if data['screenshots']['identical'] > 3:
        recommendations.append("🖼️ Variez vos tâches pour éviter les captures identiques")
    
    if data['attendance']['days_late'] > 0:
        recommendations.append("⏰ Améliorez votre ponctualité matinale")
    
    if data['tasks']['completion_rate'] < 50:
        recommendations.append("✅ Priorisez et terminez vos tâches importantes")
    
    if data['attendance']['total_work_hours'] < 35:
        recommendations.append("💪 Augmentez votre temps de travail hebdomadaire")
    
    if not recommendations:
        recommendations.append("🎉 Excellent travail ! Continuez comme ça !")
    
    return recommendations

@app.route('/api/calculate-score', methods=['POST'])
def calculate_score():
    try:
        data = request.json
        print(f"🤖 [IA] Calcul du score pour employé: {data.get('employee_id')}")
        
        score = calculate_productivity_score(data)
        recommendations = generate_recommendations(data)
        
        return jsonify({
            'success': True,
            'employee_id': data.get('employee_id'),
            'score': score,
            'level': 'Élevé' if score >= 80 else 'Moyen' if score >= 60 else 'Faible',
            'recommendations': recommendations,
            'details': {
                'productivity': score,
                'activity_score': data['activity']['avg_activity_level'],
                'tasks_score': data['tasks']['completion_rate'],
                'attendance_score': (data['attendance']['total_work_hours'] / (8 * data['attendance']['days_present'])) * 100 if data['attendance']['days_present'] > 0 else 0
            }
        })
        
    except Exception as e:
        print(f"❌ Erreur IA: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'IA Server is running'})

if __name__ == '__main__':
    print("🚀 Serveur IA démarré sur http://localhost:5001")
    app.run(host='0.0.0.0', port=5001, debug=True)