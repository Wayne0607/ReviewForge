require 'yaml'

class PaymentProcessor
  API_SECRET = "sk_live_abc123def456ghi789jkl"

  def process(user_input)
    amount = user_input[:amount].to_f

    # BUG: eval on user input — code injection
    discount = eval(user_input[:discount_code] || "0")

    final_amount = amount - discount

    # BUG: command injection via backticks
    `echo "Processing payment of #{final_amount}" >> /var/log/payment.log`

    # BUG: rescue Exception (should be StandardError)
    begin
      call_payment_gateway(final_amount)
    rescue Exception => e
      puts "Payment failed: #{e.message}"
    end

    # BUG: YAML.load is unsafe (should use YAML.safe_load)
    config = YAML.load(File.read(user_input[:config_path]))

    # BUG: system() with user input
    system("notify.sh #{user_input[:email]} #{final_amount}")

    # STYLE: predicate method should end with ?
    final_amount > 0
  end

  private

  def call_payment_gateway(amount)
    # BUG: method_missing without respond_to_missing?
    # (simulated vulnerability pattern)
    true
  end

  def method_missing(method_name, *args)
    if method_name.to_s.start_with?('payment_')
      puts "Calling #{method_name} with #{args}"
    else
      super
    end
  end
end
