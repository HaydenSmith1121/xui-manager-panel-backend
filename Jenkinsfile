pipeline {
  agent any

  parameters {
    choice(name: 'ACTION', choices: ['deploy', 'rollback'], description: 'deploy 发布新版本；rollback 回退到旧版本')
    string(name: 'TARGET_RELEASE', defaultValue: '', description: '回滚目标版本目录名；留空则回退 previous 或上一个版本')
    string(name: 'LISTEN_HOST', defaultValue: '127.0.0.1', description: '后端监听地址')
    string(name: 'LISTEN_PORT', defaultValue: '25889', description: '后端监听端口')
    string(name: 'KEEP_RELEASES', defaultValue: '5', description: '保留最近几个版本')
  }

  stages {
    stage('Validate') {
      when { expression { params.ACTION == 'deploy' } }
      steps {
        sh 'python3 -m compileall -q xui_manager tools'
      }
    }
    stage('Deploy') {
      when { expression { params.ACTION == 'deploy' } }
      steps {
        sh '''sudo env SOURCE_DIR="$WORKSPACE" RELEASE_ID="${BUILD_NUMBER}-${GIT_COMMIT}" LISTEN_HOST="${LISTEN_HOST}" LISTEN_PORT="${LISTEN_PORT}" KEEP_RELEASES="${KEEP_RELEASES}" bash deploy/jenkins-deploy.sh'''
      }
    }
    stage('Rollback') {
      when { expression { params.ACTION == 'rollback' } }
      steps {
        sh '''sudo env TARGET_RELEASE="${TARGET_RELEASE}" LISTEN_PORT="${LISTEN_PORT}" bash deploy/rollback.sh'''
      }
    }
  }
}
